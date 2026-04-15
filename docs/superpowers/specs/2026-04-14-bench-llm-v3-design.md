# scripts/bench_llm_v3.py — 设计规格

**日期**: 2026-04-14
**作者**: Allen + Claude (brainstorm)
**目的**: 为 Jarvis 语音助手做主 LLM 选型决策，通过对 8 个候选模型在中文长笔记场景下的 TTFT / recall 准确率 / cache 命中率 / 成本进行系统对照测试。
**位置**: `~/Projects/jarvis/scripts/bench_llm_v3.py`（与 v1 `bench_llm.py` 同级）

---

## 1 背景

v1 `bench_llm.py` 只测了单 prompt "加拿大首都是哪里" 的 TTFB，信号太弱。**Research Pack 4** (2026-04-14) 揭示各厂商都**不公开中文长上下文具体数据**，xAI 官方 model card 甚至零 benchmark。决策必须靠自测。

v3 的核心改进：
- 真实 Jarvis 工作流对标：中文笔记（OM 观察流格式）+ recall 任务
- Cache 行为诚实报告（揭露 OpenAI/xAI 自动 cache 的黑箱行为）
- 多运行模式，让"5 分钟做决定" 和 "过夜全量" 都能跑

## 2 目标 & 非目标

**目标**：
- 一个脚本覆盖 `--quick` / `--standard` / `--deep` / `--decision` / `--model <X>` 五种运行粒度
- 输出对 "主 LLM 选谁" 可直接决策的 Markdown 表格
- CSV 保留所有原始数据供后续分析
- 揭露各 provider 真实 cache 命中行为（而非厂商声明）

**非目标**：
- 不做 agent/tool-use benchmarking（另外脚本）
- 不做多轮对话评测（只测单轮长上下文）
- 不替代厂商官方 benchmark（MMLU / C-Eval 等）

## 3 运行模式矩阵

测量单元是 **MCT 三元组** `(model, context, task)`。每个 MCT 跑 **1 cold + 2 warm = 3 次 API call**。
总 calls = MCTs × 3。

| 模式 | 模型 | Context 分桶 | Tasks | MCT 数 | 总 calls | 耗时 | 成本 |
|---|---|---|---|---|---|---|---|
| `--quick` | Sonnet, Haiku | 2k, 10k | 全 3 | 2×2×3 = 12 | 36 | 3-5 min | <$0.30 |
| `--standard` | 全 8 | 2k, 10k, 30k, 100k | 全 3 | 8×4×3 = 96 | **288** | 15-20 min | $5-7 |
| `--deep` | 全 8 | 上 + 200k, + stale(6min) | 全 3 | ~160 | ~500 | 2-4 hr | $12-18 |
| `--decision` | Sonnet, Opus, Haiku, Grok-4.1-fast, GPT-5 | 30k only | recall only | 5×1×1 = 5 | 15 | 5-8 min | <$1 |
| `--model <name>` | 单模型 | 全 4 | 全 3 | 1×4×3 = 12 | 36 | 3-5 min | 依模型 |

**CLI 签名**：
```bash
uv run python scripts/bench_llm_v3.py [--quick | --standard | --deep | --decision | --model <name>]
                              [--dry-run]              # 仅生成 fixture + 打印 plan + 估算成本, 不调 API
                              [--with-chart]           # 生成 plotly chart.html
                              [--output-dir <path>]    # 默认 bench_results/{timestamp}/
                              [--no-confirm]           # 跳过 --deep 的确认提示
                              [--fixtures-dir <path>]  # 默认 bench_fixtures/
```

**`--dry-run` 输出示例**：
```
[DRY RUN] Would generate fixtures: 2k, 10k, 30k, 100k
[DRY RUN] Mode: --standard
[DRY RUN] Active providers (key check): ✓ anthropic ✗ openai ✓ groq ✓ xai ✓ google
[DRY RUN] Active MCT count: 7 models × 4 contexts × 3 tasks = 84 MCTs
[DRY RUN] Total API calls: 252 (minus openai since no key)
[DRY RUN] Estimated cost breakdown:
    anthropic/sonnet:  $1.82 (4 contexts × 3 tasks × 3 calls @ avg 35.5k input)
    anthropic/opus:    $4.50
    anthropic/haiku:   $0.61
    ...
    Total: $4.97 ± $1
[DRY RUN] Provider concurrency: 4 (Semaphore(1) each)
[DRY RUN] Wall-clock estimate: 12-18 min
[DRY RUN] cache_window_risk threshold: 100s per task block
```

## 4 文件布局

```
~/Projects/jarvis/
├── scripts/
│   └── bench_llm_v3.py           # 主脚本 ~600 行
├── bench_fixtures/               # 笔记固件（进 .gitignore）
│   ├── fake_notes_2k.txt         # cl100k 2000 tokens
│   ├── fake_notes_10k.txt
│   ├── fake_notes_30k.txt
│   ├── fake_notes_100k.txt
│   └── fake_notes_200k.txt       # 仅 --deep 用
└── bench_results/
    └── 2026-04-14_1530/          # timestamp 分目录
        ├── results.csv
        ├── summary.md
        ├── chart.html            # --with-chart 才生成
        └── run_meta.json         # 运行参数 + 环境信息（SDK 版本、模型 ID fallback 记录等）
```

## 5 Notes Fixture 生成

### 5.1 文件格式

每行一条观察：
```
* {emoji} ({HH:MM}) Allen {动词}: {内容}. (meaning 2026-04-{DD})
```

- `emoji` ∈ { 🟢 🟡 🟠 🔵 🟣 } (随机)
- 动词 ∈ { 观察, 提到, 记录, 体验到, 反馈 } (随机)
- 内容：从 **50 条中文观察模板池** 随机（例："早上喝了第一杯咖啡，今天 Standard Brew", "昨晚睡眠 7h 20min, Oura ring 评分 82"）
- 日期：2026-04-01 到 2026-04-13 循环

### 5.2 针 & 干扰项

**针**（必须在每个 context size 文件中出现，位置 50% ± 5%）：
```
* 🟠 (14:28) Allen 最喜欢喝拿铁，尤其是 Revolver 咖啡馆的日晒耶加雪菲豆. (meaning 2026-04-09)
```

**干扰项**（针之前 10-20 行的随机位置）：
```
* 🟢 (HH:MM) Allen 提到过同事喜欢美式咖啡. (meaning 2026-04-DD)
```

### 5.3 生成算法

```python
def generate_fake_notes(target_tokens: int, seed: int = 42) -> str:
    rng = random.Random(seed + target_tokens)  # seed + size 保证每个 size 不同但可复现
    enc = tiktoken.get_encoding("cl100k_base")
    lines: list[str] = []
    while len(enc.encode("\n".join(lines))) < target_tokens - 50:
        lines.append(_random_observation(rng))
        if len(lines) == target_tokens // 50:  # 大约 1 行 ≈ 50 tokens，预估针位置
            pass  # 留位
    # 插入针
    needle_pos = int(len(lines) * (0.5 + rng.uniform(-0.05, 0.05)))
    distractor_pos = needle_pos - rng.randint(10, 20)
    lines.insert(distractor_pos, DISTRACTOR_LINE)
    lines.insert(needle_pos + 1, NEEDLE_LINE)  # +1 因为 distractor 已 insert
    return "\n".join(lines)
```

**关键约束**：
- Seed 固定 → 每次运行生成相同文件 → Anthropic cache 可跨进程命中
- 文件已存在则**跳过生成**，直接读 → 保证多次 run 完全字节一致
- 只允许通过手动 `rm bench_fixtures/*.txt` 强制重生

### 5.4 Recall 验证

```python
def verify_recall(answer: str) -> bool:
    # 要求两个关键词同时命中: "拿铁" (咖啡类型) + 品牌词之一
    # 防止模型瞎猜 "可能是拿铁" 就算命中
    has_type = "拿铁" in answer
    has_brand = "Revolver" in answer or "耶加" in answer
    has_needle = has_type and has_brand
    # 只提干扰项 (美式咖啡) 不算命中
    false_positive = "美式" in answer and not has_needle
    return has_needle and not false_positive
```

## 6 Cache-bust 前缀（关键设计）

### 6.1 目的

OpenAI / xAI 自动 prefix cache，光靠"换 UUID"不一定能让 cold 真 cold。解决方案：**每 (provider, model, context) 生成一次 bust_prefix，3 个 task 共享**。

```python
bust_prefix = f"# Session: {uuid4().hex}\n# Timestamp: {time.time_ns()}\n\n"
```

- UUID 保证跨进程唯一
- 纳秒时间戳保证即使 UUID 冲突也不重复
- 两者组合 → 任何 provider 的自动 cache 都无法把两次不同 run 的内容识别为同一 prefix

### 6.2 用法

```python
full_prompt_prefix = bust_prefix + notes  # 构造一次
# Call 1 (task_simple, cold):    first API call，这个 prefix 从未出现 → cache miss
# Call 2 (task_recall, warm):    prefix 相同 → cache hit
# Call 3 (task_synthesis, warm): prefix 相同 → cache hit
```

### 6.3 Provider-specific 放置

| Provider | 结构 |
|---|---|
| Anthropic | `system=[{"type":"text","text":full_prompt_prefix,"cache_control":{"type":"ephemeral"}}]`, messages=`[{"role":"user","content":task}]` |
| OpenAI / xAI / Groq | `messages=[{"role":"system","content":full_prompt_prefix},{"role":"user","content":task}]`（自动前缀 cache） |
| Gemini | 如 `len(notes) ≥ provider_min_cache_tokens`, 用 `CachedContent.create(system_instruction=full_prompt_prefix)` 然后 `model.generate_content(task, cached_content=cache)`；否则 inline 送，不尝试 cache |

## 7 任务定义

```python
TASKS = {
    "simple":     "今天温哥华天气怎么样？",  # 不依赖 notes，测纯 TTFT
    "recall":     "根据以上观察，Allen 最喜欢喝的咖啡是什么？请用一句话回答。",
    "synthesis":  "根据以上观察，Allen 最近一个月的生活状态如何？给出 3 点简短建议。",
}
```

- `simple`: recall 不需要 notes，但我们照样送 notes 进 context → 测的是"大 context 下的小问题 TTFT"，贴近 Jarvis 真实场景
- `recall`: 唯一做答案正确性判定的任务
- `synthesis`: 开放式生成，只记录 TTFT/tokens，不判对错

## 8 执行引擎

### 8.1 并发结构

```python
# 跨 provider 并发；per-provider Semaphore(1) 保证一个 provider 同时只跑一个 MC 块
# （避免同 provider 内 rate limit + 保证每 task 的 3 次 cold/warm 背靠背不被打断）
provider_sems: dict[str, asyncio.Semaphore] = {
    p.name: asyncio.Semaphore(1) for p in PROVIDERS
}

async def run_all_mc_blocks():
    tasks = []
    for provider in PROVIDERS:
        for model in provider.models:
            for ctx_size in active_context_sizes:
                tasks.append(run_one_mc_block(provider, model, ctx_size, provider_sems[provider.name]))
    return await asyncio.gather(*tasks, return_exceptions=True)
```

### 8.2 单 MCT 三元组的 1 cold + 2 warm 采样

原子单位：每个 task 独立生成自己的 `bust_prefix`，3 个 task 各跑 1 cold + 2 warm = 3 次 API call。
per-provider Semaphore(1) 保证同一 provider 的 (model, context) 顺序跑（不同 provider 并发）。
单 MC 块: 3 tasks × 3 calls = **9 calls**。全量 --standard = 32 MC × 9 = **288 calls**。

```python
async def run_one_mc_block(provider, model, ctx_size, sem):
    """Run 3 tasks × (1 cold + 2 warm) for one (model, context) pair."""
    async with sem:  # per-provider 互斥
        notes = load_notes(ctx_size)
        all_results = []

        for task_name in ["simple", "recall", "synthesis"]:
            bust_prefix = make_bust_prefix()  # 每 task 一个新 prefix → 保证 cold 真 cold
            full_prefix = bust_prefix + notes
            task_start = time.monotonic()

            task_results = []
            for run_idx in range(3):
                cache_state = "cold" if run_idx == 0 else "warm"
                r = await call_api_with_retry(
                    provider, model, full_prefix, task_name, cache_state
                )
                r.update({
                    "model": model.id,
                    "provider": provider.name,
                    "nominal_tokens_cl100k": ctx_size,
                    "task": task_name,
                    "cache_state": cache_state,
                    "run_idx": run_idx,
                })
                task_results.append(r)

            task_elapsed = time.monotonic() - task_start
            # 阈值 100s: Anthropic 真实 TTL 是 300s (5 min), 用 3x margin
            # 理论上 cold+warm+warm 三次 back-to-back 不该超 ~90s 即便 100k Opus
            cache_window_risk = task_elapsed > 100.0
            if cache_window_risk:
                LOGGER.warning(
                    f"{model.id} ctx={ctx_size} task={task_name}: "
                    f"block took {task_elapsed:.1f}s (>100s), warm cache may have "
                    f"started expiring (Anthropic 5min TTL)"
                )
            for r in task_results:
                r["cache_window_risk"] = cache_window_risk
            all_results.extend(task_results)

        return all_results
```

**为什么每 task 新 prefix**：
- 若 3 个 task 共享一个 prefix，`task_recall` 的 cold call 实际上会命中 `task_simple` 留下的 cache → 数据污染
- 每 task 新 prefix 隔离三个 task 的 cold/warm 对比
- 代价：生成 prefix 几乎免费（仅增加约 30 tokens / task 的输入量）

### 8.3 API 调用 & 重试

```python
async def call_api_with_retry(provider, model, full_prefix, task_name, cache_state):
    query = TASKS[task_name]
    for attempt in range(4):  # 1 原始 + 3 重试
        try:
            return await call_api(provider, model, full_prefix, query, cache_state)
        except RateLimitError:
            wait = 2 ** attempt  # 1s, 2s, 4s, 8s
            LOGGER.warning(f"Rate limited {model.id}, wait {wait}s")
            await asyncio.sleep(wait)
        except (InternalServerError, ServiceUnavailableError):
            await asyncio.sleep(2 ** attempt)
        except (AuthenticationError, BadRequestError) as e:
            return {"error": str(e)[:200], "ttft_ms": -1, ...}
        except asyncio.TimeoutError:
            return {"error": "timeout", "ttft_ms": -1, ...}
    return {"error": "max retries exceeded", "ttft_ms": -1, ...}
```

单调用超时：90 秒（长 context 100k 场景预留）。

### 8.4 Warmup

启动时对每个 model 发 1 次小 prompt：**完全独立的 payload，零字节与正式测试共享**，防止污染后续 cold 测量（OpenAI/xAI 自动 prefix cache 会把 warmup 的 system prompt 当作 prefix 缓存住）。

**Warmup payload 标准**：
```python
async def warmup(provider, model):
    # 纯 user message, 无 system prompt, 无 notes 相关内容
    return await call_api_simple(
        provider, model,
        system=None,       # ← 关键：不设 system, 避免前缀污染
        user="Just say: OK",
        max_tokens=5,
    )
```

目的：
1. 验证 API key + model ID 存活
2. 触发 provider 端 cold-start (网络握手, TLS 复用)
3. 测量结果不计入 CSV

失败则按 `MODEL_CATALOG` 中的 fallback 列表依次尝试。全部失败 → 记录 warning 并从 active list 移除。

## 9 CSV Schema

```python
CSV_FIELDS = [
    "timestamp",                  # ISO 8601
    "model",                      # primary_id or fallback_id
    "model_is_fallback",          # bool
    "provider",
    "nominal_tokens_cl100k",      # 2000/10000/30000/100000
    "actual_input_tokens_api",    # provider 自报 (不含 cache, 见下方分段)
    "task",                       # simple/recall/synthesis
    "cache_state",                # cold/warm/stale
    "run_idx",                    # 0 (cold), 1 (warm1), 2 (warm2)
    "ttft_ms",
    "total_ms",
    "output_tokens",
    "tokens_per_second",
    "answer",                     # 截断到 500 字
    "answer_correct",             # bool；非 recall 任务填 None
    "cache_actually_hit",         # bool
    "cache_write_tokens",         # int - Anthropic cache_creation_input_tokens, 其他 provider 为 0
    "cache_read_tokens",          # int - provider 返回的命中 tokens (原 cache_hit_tokens)
    "cache_hit_ratio",            # float, cache_read_tokens / total_prompt_tokens
    "cost_usd",                   # float, 基于三段计价 (regular + write + read)
    "cache_window_risk",          # bool
    "error",                      # str or empty
]
```

**Cache 指标抓取**（每 provider response 里的字段）：

| Provider | cache_write (1.25x) | cache_read (0.1-0.5x) |
|---|---|---|
| Anthropic | `response.usage.cache_creation_input_tokens` | `response.usage.cache_read_input_tokens` |
| OpenAI | 无 (自动 cache, 不额外收 write 费) | `response.usage.prompt_tokens_details.cached_tokens` |
| xAI Grok | 无 | `response.usage.prompt_tokens_details.cached_tokens` |
| Gemini | 无 (CachedContent 有独立创建费, 但按次 + 存储时长, 此脚本不算) | `response.usage_metadata.cached_content_token_count` |
| Groq | 无 cache | 永远 0 |

`cache_actually_hit = cache_read_tokens > 0`。

**注意**：Anthropic 的 `actual_input_tokens_api` 是三段相加 (input + cache_creation + cache_read)，CSV 里的 `actual_input_tokens_api` 记录的是 total prompt tokens（即总送进去的 token 数），`cache_write_tokens` 和 `cache_read_tokens` 分别从 response 对应字段拿。OpenAI/xAI/Gemini 的 `prompt_tokens` 已经是 total, cache_read_tokens 是 total 的子集（cached_tokens 属于 prompt_tokens 的一部分）。

## 10 Model Catalog

```python
@dataclass
class ModelSpec:
    provider: str
    primary_id: str
    fallback_ids: list[str]
    input_price_per_1m: float     # USD, base input price
    output_price_per_1m: float
    cache_write_multiplier: float  # Anthropic = 1.25, 其他 = 1.00 (无独立 write 费)
    cache_read_multiplier: float   # 0.10 = cache hit 是 base 的 10%
    min_cache_tokens: int          # 触发 cache 的最小 token 阈值

MODEL_CATALOG = [
    # Anthropic: 显式 cache, write 1.25x, read 0.10x
    ModelSpec("anthropic", "claude-sonnet-4-6",           ["claude-sonnet-4-5"],       3.00,  15.00, 1.25, 0.10, 1024),
    ModelSpec("anthropic", "claude-opus-4-6",             ["claude-opus-4-5"],        15.00,  75.00, 1.25, 0.10, 1024),
    ModelSpec("anthropic", "claude-haiku-4-5-20251001",   ["claude-haiku-4-5"],        1.00,   5.00, 1.25, 0.10, 1024),
    # OpenAI: 自动 cache, 无独立 write 费, read 0.50x
    ModelSpec("openai",    "gpt-5",                       ["gpt-4o"],                  2.50,  10.00, 1.00, 0.50, 1024),
    ModelSpec("openai",    "gpt-5-mini",                  ["gpt-4o-mini"],             0.15,   0.60, 1.00, 0.50, 1024),
    # Gemini: CachedContent 的 storage 费本脚本不算, read 0.25x
    ModelSpec("google",    "gemini-3-pro-preview",        ["models/gemini-3-pro-preview", "gemini-2.5-pro", "models/gemini-2.5-pro"],
                                                                                       1.25,   5.00, 1.00, 0.25, 4096),
    # xAI: 自动 cache, 无独立 write 费, read 0.25x (2025 推测)
    ModelSpec("xai",       "grok-4-1-fast-non-reasoning", ["grok-4"],                  0.20,   0.50, 1.00, 0.25, 1024),
    # Groq: 不支持 cache (永远按 regular 价计)
    ModelSpec("groq",      "llama-3.3-70b-versatile",     [],                          0.59,   0.79, 1.00, 1.00, 999_999),
]
```

`cost_usd` 三段计价：
```python
def calc_cost(
    cache_write_tokens: int,
    cache_read_tokens: int,
    prompt_total_tokens: int,  # 含 cache 部分
    output_tokens: int,
    spec: ModelSpec,
) -> float:
    # 非 cache 部分 = total - write - read (write 和 read 互斥)
    regular_input = prompt_total_tokens - cache_write_tokens - cache_read_tokens
    cost  = regular_input      * spec.input_price_per_1m /  1e6
    cost += cache_write_tokens * spec.input_price_per_1m * spec.cache_write_multiplier / 1e6
    cost += cache_read_tokens  * spec.input_price_per_1m * spec.cache_read_multiplier  / 1e6
    cost += output_tokens      * spec.output_price_per_1m / 1e6
    return cost
```

**关键**：Anthropic 的 **cache_creation 首次写入贵 1.25x**，必须单列计算。不然 cold 调用（真·建 cache 的那一次）会被低估 ~12.5%。

## 11 Summary.md 输出

固定 5 张表：

1. **TTFT × Context (warm cache, avg ms, n=2)** — 主要日常响应速度指标。注：warm 只有 2 个样本（run 1 + 2），用 avg 而非 median；如果 2 个样本差 >30%，额外标注 `(unstable)`
2. **Recall 准确率 × Context** — OM 硬指标（用 §5.4 的严格双关键词判定）
3. **Cold vs Warm TTFT (100k)** — cache 加速比；warm 用 2 样本 avg
4. **真实 Cache 命中率** — 揭露黑箱；分 write/read 两栏（Anthropic 独有 write）
5. **成本对比 ($/100 次 30k 对话)** — 月预算估算；cold 和 warm 分列展示

表以 `nominal_tokens_cl100k` 分桶（而非 actual）以保持跨 provider 列对齐。正文注解列出各 provider 的 `actual / nominal` 比例（Gemini 典型 ~1.3，Claude ~1.2）。

## 12 chart.html（可选）

`--with-chart` 触发。Plotly 生成单个 HTML：
- 分面网格：行 = cache_state (cold/warm)，列 = context size
- 每格：分组柱状图，x = model, y = TTFT ms
- 颜色：provider
- Hover: 显示 cost / recall_correct / cache_hit_ratio

## 13 环境 & 依赖

### 13.1 自举依赖（PEP 723 inline script metadata）

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "anthropic>=0.42",
#   "openai>=1.50",
#   "google-generativeai>=0.8",
#   "groq>=0.11",
#   "tiktoken>=0.8",
#   "tqdm>=4.66",
#   "plotly>=5.18",
#   "pandas>=2.0",
# ]
# ///
```

用户 `uv run scripts/bench_llm_v3.py` 自动解析 + 装依赖，无需额外 `pyproject.toml`。

### 13.2 API Key 加载

```python
KEYS = {
    "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
    "openai":    os.environ.get("OPENAI_API_KEY"),
    "google":    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
    "groq":      os.environ.get("GROQ_API_KEY"),
    "xai":       os.environ.get("XAI_API_KEY"),
}
```

启动时打印：
```
✓ anthropic  ✗ openai (missing ANTHROPIC_API_KEY)  ✓ groq  ✓ xai  ✓ google
```

缺 key 的 provider 的所有模型自动从 active list 移除。

## 14 实时进度输出

tqdm + 简短分模型行：
```
[15:30:12] ▸ Loading fixtures... (4 files, 144 KB total)
[15:30:13] ▸ Warmup: ✓ Sonnet ✓ Opus ✓ Haiku ✓ gpt-5 ✓ gpt-5-mini
                     ✗ gemini-3-pro-preview → fallback gemini-2.5-pro ✓
                     ✓ Grok ✓ Llama
[15:30:45] Running --standard: 96 MCTs × 3 calls = 288 API calls, 8 providers parallel
Progress: ████████░░ 78% | Elapsed: 14m 22s | ETA: 3m 12s | Errors: 2 | Cost: $4.13
  ↳ [anthropic/opus @ 100k / recall]  cold 2847ms → warm 1182/1201ms ✓ recall ✓
  ↳ [groq/llama @ 30k / recall]       cold  187ms → warm  189/192ms (no cache) ✗ recall
  ↳ [xai/grok @ 30k / recall]         cold 2100ms → warm 2080/2075ms (cache=0%!) ✓ recall
```

## 15 测试 & 上线流程

### 15.1 Unit tests（新建 `tests/test_bench_llm_v3.py`，纳入项目 pytest）

- `test_generate_fake_notes_deterministic`: 同 seed 生成结果 byte-equal
- `test_needle_position_within_tolerance`: 针位置在 45%-55% 之间
- `test_distractor_precedes_needle`: 干扰项在针之前
- `test_verify_recall`: 针对 5 个合成 answer 测正确性判定（必测: "拿铁" 单独命中不算、"美式" 干扰项不干扰、双关键词才算真命中）
- `test_cost_calculation_anthropic`: 构造含 `cache_creation_input_tokens=5000` + `cache_read_input_tokens=25000` 的假 response，验算三段计价公式
- `test_cost_calculation_groq_nocache`: Groq response cache_read=0, 验证全 regular 价
- `test_cache_metrics_extraction`: 对 5 个 provider 的假 response 字典，验证正确提取 cache_write/cache_read tokens

### 15.2 上线强制流程（不可跳步）

**v2 教训**：没做 smoke test 直接跑全量 → 36/72 Anthropic 全 400 失败，浪费 API quota。v3 强制分三阶段：

#### Stage 1: `--dry-run --quick`
```bash
uv run python scripts/bench_llm_v3.py --dry-run --quick
```
验收：
- [ ] `bench_fixtures/fake_notes_{2k,10k}.txt` 已生成 + 针位置 45-55% + 干扰项在针前 10-20 行
- [ ] API key 检测报告正确（缺的 ✗, 有的 ✓）
- [ ] 估算成本 < $0.30
- [ ] Plan 里 MCT 数 = 12（2 × 2 × 3），total calls = 36

#### Stage 2: `--quick` 真跑（36 calls, <$0.30）
```bash
uv run python scripts/bench_llm_v3.py --quick
```

**强制调试点 — Anthropic cache_control 验证**：
脚本必须在**第一次 Anthropic 调用后立即**打印原始 usage 字段（每次 run 只打一次）：

```python
# 在 call_api_anthropic 里，首次调用时:
if not ANTHROPIC_DEBUG_PRINTED_ONCE:
    print("=" * 78)
    print(f"ANTHROPIC FIRST CALL RESPONSE (DEBUG) — {model_id}")
    print(f"  usage.input_tokens:                 {resp.usage.input_tokens}")
    print(f"  usage.cache_creation_input_tokens:  {resp.usage.cache_creation_input_tokens}")
    print(f"  usage.cache_read_input_tokens:      {resp.usage.cache_read_input_tokens}")
    print(f"  usage.output_tokens:                {resp.usage.output_tokens}")
    print("=" * 78)
    ANTHROPIC_DEBUG_PRINTED_ONCE = True
```

**验收**：
- [ ] 首次 cold call: `cache_creation_input_tokens > 0` 且 `cache_read_input_tokens == 0`（cache 写成功）
- [ ] 随后同 prefix warm call: `cache_read_input_tokens > 0`（cache 读取成功）

**如果 `cache_creation_input_tokens == 0`**：
→ `cache_control` 语法错了（v2 就是这样死的），**停止上 --standard**，先修 bug

**`--quick` 成功条件**：
- [ ] 36 calls 全部返回（容忍 ≤2 个 rate limit skip）
- [ ] CSV 有 36 行
- [ ] 至少 1 个模型 recall 正确
- [ ] 总耗时 < 5 分钟
- [ ] 总成本 < $0.50

#### Stage 3: `--standard` 全量（仅 Stage 2 全绿后允许）
```bash
uv run python scripts/bench_llm_v3.py --standard
```

**强制前置检查**：启动时扫描 `bench_results/*/run_meta.json`，若找不到 `mode=quick` 且 `errors_total ≤ 2` 的记录，**拒绝启动**并打印 "请先跑 --quick 验证"。可用 `--force-standard` 覆盖（自担风险）。

### 15.3 --decision 验收

- 至少 2 个 Anthropic 模型有有效数据
- recall 准确率给出可区分的分数（不全 100% 也不全 0%）

## 16 开放问题 / 运行时决策

1. **Model ID 时效性**：`MODEL_CATALOG` 中的 primary_id 可能过时。依赖 warmup 阶段的 fallback 机制。如有新模型 ID 发布，更新 catalog 即可。
2. **Pricing 时效性**：价格表内嵌。上线时校准一次，每季度复查。写明 `PRICING_SNAPSHOT_DATE = "2026-04-14"` 在代码顶部。
3. **Gemini CachedContent 最小 token**：官方文档 Flash=32k, Pro=4k（可能随版本变）。若 API 返回 400 cache-too-small，自动降级为 inline 送（标记 `cache_actually_hit=False`）。
4. **xAI Grok 是否支持 prompt cache**：v1 bench 里能跑但无 cache 信号。代码对 xAI 假设自动 cache（OpenAI 兼容），若 response 无 `cached_tokens` 字段则报 0。

## 17 与 v1 的关系

v1 `bench_llm.py` **保留不删**，作为"跨 9 个 Llama-3.3-70B 部署商的 TTFB-only 快速对比"工具。v3 是新的更完整的测试，不覆盖 v1 的场景。

---

**设计确认**：
- [x] C + D 运行模式（quick / standard / deep / decision + single-model）+ `--dry-run`
- [x] A 诚实 cache 报告：`cache_write_tokens` + `cache_read_tokens` + `cache_hit_ratio` + `cache_actually_hit` 四列
- [x] **三段计价**（regular + cache_write 1.25x + cache_read 0.1-0.5x），修复 Anthropic cold 低估
- [x] 1 cold + 2 warm 采样（288 calls for --standard）
- [x] UUID + 纳秒时间戳 cache-bust prefix，**每 task 独立**
- [x] 跨 provider 并发 + per-provider Semaphore(1) + **100s cache_window_risk 断言**（Anthropic 300s TTL 的 3x margin）
- [x] token 策略 C（cl100k 名义分桶 + actual_input_tokens_api 真值 + 成本用真值 +各 provider 三段 tokens）
- [x] Warmup **独立 payload**（零前缀共享），防止 OpenAI/xAI 自动 cache 污染首次 cold
- [x] Recall 严格双关键词（"拿铁" AND "Revolver/耶加"）
- [x] Summary 标注 warm n=2 avg（非 median）
- [x] Gemini 多级 fallback (`primary` → `models/primary` → `gemini-2.5-pro` → `models/gemini-2.5-pro`)

**下一步**：交棒给 `writing-plans` 生成详细实施计划。
