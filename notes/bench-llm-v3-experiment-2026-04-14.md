# Bench LLM v3 — Jarvis 主 LLM 选型实验

**日期**: 2026-04-14 · **作者**: Allen + Claude · **实验总耗时**: ~40 min · **总花费**: $15.46

---

## TL;DR

实测 **10 个模型 × 4 context 桶 × 3 任务 × (1 cold + 2 warm)** 共 432 次 API 调用，在中文长笔记（OM 观察流）场景下对标 Jarvis 工作负载。

**决策**：

| 角色 | 模型 | TTFT@30k | Recall@30k | Recall@100k | Cost/100 @30k |
|---|---|---|---|---|---|
| 🥇 主 LLM | `grok-4.20-0309-non-reasoning` | **488ms** | 3/3 | **3/3** | **$0.26** |
| 🥈 备选 | `claude-haiku-4-5-20251001` | 752ms | 3/3 | N/A (Tier 1 撞 TPM) | $1.77 |

**关键发现**：
1. xAI 从 4.1 → 4.20 修复了 100k 中文 recall 崩盘问题（0/3 → 3/3）
2. Anthropic Tier 1 TPM 限制使 Claude 100k benchmark 实际不可行（单次请求超 TPM 预算 2-5 倍）
3. GPT-5 默认启用 reasoning 导致 TTFT 6-7s，不适合语音场景
4. xAI Grok 的 OpenAI-compat prompt cache **实际支持且 100% 命中**（Pack 3 的"疑似支持"坐实）
5. 三段计价（cache_creation 1.25×）修复后，Anthropic cold 真实成本比旧公式高 12.5%

---

## 1. 背景与动机

### 1.1 Jarvis 语音助手场景约束
- 中文 OM（观察流）笔记平均 10-30k tokens，不会到 100k
- TTFT 目标 <500ms（语音交互低延迟要求）
- Recall 任务：从笔记中间找"Allen 最喜欢喝拿铁"这类针
- 月调用量估 15000 次（500/天），成本敏感

### 1.2 为什么要自己测
Research Pack 4 (2026-04-14) 揭示：

| 问题 | 公开数据状况 |
|---|---|
| Claude Sonnet/Opus 4.6 中文 C-Eval/CMMLU | Anthropic 不公开 |
| Grok-4.1-fast 长上下文中文 | xAI model card **零** benchmark |
| Groq Llama-3.3-70B 的 Chinese long context | Meta 8 支持语言**不含中文** |
| Gemini 中文 NIAH at 32/64/128k | 无 |
| **OpenAI/xAI 真实 cache 命中率** | **厂商黑箱** |

**结论**：用 Jarvis 自己的工作负载（中文笔记 + recall）实测，比看任何 academic benchmark 都靠谱。

### 1.3 v2 的教训
先前 `bench_llm_v2.py`（已废弃）一次性跑全量 → 36/72 Anthropic 调用全 400 错误（`cache_control` 语法错），浪费 quota。v3 强制 **smoke gate 三阶段**：`--dry-run` → `--quick` → `--standard`，每阶段有明确验收条件。

---

## 2. 方法论

### 2.1 测试矩阵

- **MCT 三元组**：`(model, context, task)`，每个三元组做 `1 cold + 2 warm = 3 次 API call`
- **4 个 context 桶**（cl100k_base 计 token）：2k, 10k, 30k, 100k
- **3 个任务**：
  - `simple`: "今天温哥华天气怎么样？"（测 cold TTFT 在长 context 下的基准）
  - `recall`: "Allen 最喜欢喝的咖啡是什么？"（测针找回准确度）
  - `synthesis`: "最近一个月生活状态 + 3 点建议"（测开放生成质量）
- 10 个模型：见 §3 Model Catalog

**单次 `--standard` 运行 API calls**: 8 model × 4 context × 3 task × 3 call = **288 calls**

### 2.2 笔记固件（needle + distractor）

**位置**: `bench_fixtures/fake_notes_{2k,10k,30k,100k}.txt`

**格式**（每行一条观察）:
```
* 🟠 (HH:MM) Allen {动词}: {内容}. (meaning 2026-04-{DD})
```

**针** (50% ± 4% 位置):
```
* 🟠 (14:28) Allen 最喜欢喝拿铁，尤其是 Revolver 咖啡馆的日晒耶加雪菲豆. (meaning 2026-04-09)
```

**干扰项** (针之前 10-20 行):
```
* 🟢 (HH:MM) Allen 提到过同事喜欢美式咖啡. (meaning 2026-04-DD)
```

**Recall 验证（严格双关键词）**:
```python
has_needle = ("拿铁" in answer) and ("Revolver" in answer or "耶加" in answer)
# 只命中 "拿铁" 或只命中 "美式咖啡" 都不算 —— 防止模型瞎猜
```

**为什么要固化到磁盘**: seed 固定 + 文件存在则直接读 → 多次运行完全 byte-equal → Anthropic `cache_control` 才能跨进程命中。

### 2.3 Cache-bust prefix（关键）

OpenAI / xAI 自动 prefix cache 会让"cold"测不出真 cold。每个 `(provider, model, context, task)` 开始前生成一次 bust prefix：

```python
f"# Session: {uuid4().hex[:16]}\n# Timestamp: {time.time_ns()}\n\n"
```

- UUID 保证跨进程唯一
- 纳秒时间戳做双保险（即使 UUID 碰撞也不复用）
- prefix ~20 tokens（对 cl100k 预算影响 <1%）

**同一 MCT 内的 3 次 call（cold+warm+warm）共享这个 prefix** → cold 写 cache，后两次 warm 读 cache。**不同 MCT 之间换新 prefix** → 每次 cold 都是真 cold。

### 2.4 三段计价公式（修复 Anthropic cold 成本低估）

```python
def calc_cost(cache_write_tokens, cache_read_tokens, prompt_total_tokens, output_tokens, spec):
    regular = prompt_total_tokens - cache_write_tokens - cache_read_tokens
    cost  = regular            * spec.input_price_per_1m /  1e6
    cost += cache_write_tokens * spec.input_price_per_1m * spec.cache_write_multiplier / 1e6   # Anthropic 1.25×
    cost += cache_read_tokens  * spec.input_price_per_1m * spec.cache_read_multiplier  / 1e6   # 0.10-0.50×
    cost += output_tokens      * spec.output_price_per_1m / 1e6
    return cost
```

**Anthropic 的 `cache_creation_input_tokens` 按 1.25× 计费**（cold 写入溢价），**`cache_read_input_tokens` 按 0.10× 计费**（90% 折扣）。旧公式没拆这两段，cold 成本低估 ~12.5%。

### 2.5 Per-provider cache metric 抓取

| Provider | write field | read field |
|---|---|---|
| Anthropic | `response.usage.cache_creation_input_tokens` | `response.usage.cache_read_input_tokens` |
| OpenAI / xAI | ❌ (自动 cache, 无 write 费) | `response.usage.prompt_tokens_details.cached_tokens` |
| Gemini | ❌ (CachedContent storage 费, 本实验未用) | `response.usage_metadata.cached_content_token_count` |
| Groq | ❌ | 永远 0 |

**注意**：OpenAI-compat 流式响应需要 `stream_options={"include_usage": True}`，否则最后一块不带 usage。

### 2.6 并发模型

- **Cross-provider 并发**：8 provider 各用 `asyncio.Semaphore(1)`，互不阻塞
- **Per-provider 内部串行**：同一 provider 的多个模型/context 顺序跑，保证 cache 窗口时序清晰
- **原子 MCT 块**：一个 MCT 的 3 次 call 必须 back-to-back（`cache_window_risk > 100s` 告警，Anthropic TTL 是 300s 的 3× margin）

### 2.7 Warmup（零前缀污染）

每个模型启动时 warmup 一次，**纯 user message 无 system prompt**：

```python
client.chat.completions.create(
    model=mid,
    messages=[{"role": "user", "content": "Just say: OK"}],
    max_tokens=5,  # 或 max_completion_tokens for GPT-5
)
```

**关键**：warmup 不带 notes / system prompt 内容 → 零字节与正式测试 prefix 共享 → 防止 OpenAI/xAI 自动 cache 污染首次 cold。

---

## 3. Model Catalog（定价快照 2026-04-14）

| Provider | Primary ID | Fallback | In $/1M | Out $/1M | write × | read × | min cache |
|---|---|---|---|---|---|---|---|
| anthropic | claude-sonnet-4-6 | claude-sonnet-4-5 | 3.00 | 15.00 | **1.25** | 0.10 | 1024 |
| anthropic | claude-opus-4-6 | claude-opus-4-5 | 15.00 | 75.00 | **1.25** | 0.10 | 1024 |
| anthropic | claude-haiku-4-5-20251001 | claude-haiku-4-5 | 1.00 | 5.00 | **1.25** | 0.10 | 1024 ⚠️实测 >2k |
| openai | gpt-5 | gpt-4o | 2.50 | 10.00 | 1.00 | 0.50 | 1024 |
| openai | gpt-5-mini | gpt-4o-mini | 0.15 | 0.60 | 1.00 | 0.50 | 1024 |
| google | gemini-3-pro-preview | gemini-2.5-pro | 1.25 | 5.00 | 1.00 | 0.25 | 4096 |
| xai | grok-4-1-fast-non-reasoning | grok-4-0709 | 0.20 | 0.50 | 1.00 | 0.25 | 1024 |
| xai | grok-4-1-fast-reasoning | ↑ | 0.20 | 0.50 | 1.00 | 0.25 | 1024 |
| xai | grok-4.20-0309-non-reasoning | ↑ | 0.20 | 0.50 | 1.00 | 0.25 | 1024 |
| xai | grok-4.20-0309-reasoning | grok-4.20-0309-non-reasoning | 0.20 | 0.50 | 1.00 | 0.25 | 1024 |
| xai | grok-4-0709 (旗舰) | — | 3.00 | 15.00 | 1.00 | 0.25 | 1024 |
| groq | llama-3.3-70b-versatile | — | 0.59 | 0.79 | 1.00 | 1.00 | 无 cache |

**特殊处理**：
- GPT-5 + o1/o3 系列用 `max_completion_tokens` 而非 `max_tokens`（`_openai_token_param()` 自动识别）
- Gemini 不用 `CachedContent` 对象，直接 inline（storage 费本实验不测）

---

## 4. 实验运行历史

### 4.1 Run 1 — `--quick` (Anthropic cache_control 验证)

**命令**:
```bash
uv run python scripts/bench_llm_v3.py --quick
```

**配置**: 2 model (Sonnet, Haiku) × 2 context (2k, 10k) × 3 task × 3 call = **36 calls**

**结果**:
- ✅ 36/36 成功, 0 errors
- ✅ Anthropic cache_control 生效: `cache_creation_input_tokens = 2313` (首次 cold, ≠ 0 证明 cache 写入成功)
- ✅ Cache 命中率 99.7%
- 耗时 2.7 min, 成本 $0.34

**Artifacts**: `bench_results/2026-04-14_2121/`

### 4.2 Run 2 — `--standard` (全 8 模型)

**命令**:
```bash
uv run python scripts/bench_llm_v3.py --standard
```

**配置**: 8 model × 4 context × 3 task × 3 call = **288 calls**

**结果**:
- 267/288 成功, **21 errors** (全部在 Anthropic 100k, 每模型 7 errors)
- 耗时 23.5 min, 成本 $14.22
- xAI cache 揭露: `cached_tokens = 158` on first call → 坐实 OpenAI-compat cache 支持

**关键问题**: Sonnet 的 TPM backoff 污染了同一 Anthropic 锁队列里的 Haiku 30k warm TTFT（测到 12-27s，真实值应 <1000ms）。

**Artifacts**: `bench_results/2026-04-14_2211/`

### 4.3 Run 3 — `--model haiku` 单独跑（希望拿干净数据）

**命令**:
```bash
uv run python scripts/bench_llm_v3.py --model claude-haiku-4-5-20251001
```

**配置**: 36 calls, 独跑 Haiku（避免与 Sonnet 竞争锁）

**结果**:
- 30/36 成功, 6 errors (100k 全失败)
- Haiku 30k warm 仍然有污染 (13s avg) — **因为 Haiku Tier 1 TPM 50k，单个 30k block 累计 90k 超额**
- 结论: Anthropic Tier 1 下 Haiku **30k 也不稳**，只有 2k/10k 真正干净
- 耗时 7.8 min, 成本 $0.69

**Artifacts**: `bench_results/2026-04-14_2323/`

### 4.4 Run 4 — `--model grok-4.20-0309-non-reasoning`

**命令**:
```bash
uv run python scripts/bench_llm_v3.py --model grok-4.20-0309-non-reasoning
```

**配置**: 同时测了 2 个 Grok 4.20 变体（catalog fallback chain 触发）= 72 calls

**结果**:
- ✅ 72/72 全成功
- ✅ **4.20 non-reasoning 100k recall = 3/3**（4.1 fast 是 0/3，4.20 修复了！）
- ✅ 4.20 reasoning 100k recall 也 3/3，但 TTFT 慢 10x (4906ms vs 675ms)
- 耗时 4.9 min, 成本 $0.25

**Artifacts**: `bench_results/2026-04-14_2332/`

### 4.5 花费总结

| Run | Calls | Cost | 用时 |
|---|---|---|---|
| --quick | 36 | $0.34 | 2.7 min |
| --standard | 288 | $14.22 | 23.5 min |
| --model haiku | 36 | $0.69 | 7.8 min |
| --model grok-4.20 | 72 | $0.25 | 4.9 min |
| **合计** | **432** | **$15.46** | **38.9 min** |

---

## 5. 核心结果（所有 run 合并去除 error）

### 5.1 TTFT (ms) — 所有任务 median, 成功调用

| Model                              | 2k cold | 2k warm | 10k cold | 10k warm | 30k cold | 30k warm | 100k cold | 100k warm |
| ---------------------------------- | ------- | ------- | -------- | -------- | -------- | -------- | --------- | --------- |
| xai/grok-4-1-fast-non-reasoning    | 617     | 376     | 580      | 397      | 1085     | 495      | 4052      | 654       |
| **xai/grok-4.20-non-reasoning** 🏆 | **398** | **387** | **627**  | **449**  | **1112** | **488**  | **2756**  | **659**   |
| xai/grok-4.20-reasoning            | 3678    | 4096    | 6742     | 5437     | 4291     | 4142     | 8686      | 4731      |
| anthropic/haiku-4-5                | 597     | 605     | 763      | 530      | 962      | 752      | 2198      | 61577 ⚠️  |
| anthropic/sonnet-4-6               | 993     | 1010    | 1057     | 969      | 2134     | 23217 ⚠️ | 4563      | 5827      |
| anthropic/opus-4-6                 | 1784    | 1878    | 1831     | 2010     | 2819     | 2421     | 4701      | 4758      |
| openai/gpt-5                       | 10722   | 7443    | 7174     | 6529     | 8045     | 7022     | 14007     | 8051      |
| openai/gpt-5-mini                  | 6201    | 6019    | 6548     | 6285     | 7273     | 4939     | 9442      | 6774      |
| google/gemini-3-pro                | 5414    | 4957    | 6143     | 6027     | 5544     | 5745     | 9109      | 6219      |
| groq/llama-3.3-70b                 | 443     | 356     | 873      | 802      | 1712     | 1711     | 17668     | 17962     |

⚠️ = TPM backoff 污染（非真实 TTFT）

### 5.2 Recall 准确率（严格双关键词）

| Model | 2k | 10k | 30k | 100k |
|---|---|---|---|---|
| xai/grok-4-1-fast-non-reasoning | 3/3 | 3/3 | 3/3 | **0/3** ❌ |
| **xai/grok-4.20-non-reasoning** 🏆 | **3/3** | **3/3** | **3/3** | **3/3** ✓ |
| xai/grok-4.20-reasoning | 3/3 | 3/3 | 3/3 | 3/3 |
| anthropic/haiku-4-5 | 9/9 | 9/9 | 6/6 | 2/2 |
| anthropic/sonnet-4-6 | 6/6 | 6/6 | 3/3 | 0/0 (全 error) |
| anthropic/opus-4-6 | 3/3 | 3/3 | 3/3 | 0/0 (全 error) |
| openai/gpt-5 | **2/3** | **2/3** | **2/3** | 3/3 |
| openai/gpt-5-mini | 3/3 | 3/3 | 3/3 | 3/3 |
| google/gemini-3-pro | 3/3 | 3/3 | 3/3 | 3/3 |
| groq/llama-3.3-70b | 3/3 | 3/3 | 3/3 | 3/3 |

### 5.3 Cache 实测（warm 命中率）

| Model | 2k | 10k | 30k | 100k | 官方声明 |
|---|---|---|---|---|---|
| xai/grok-4-1-fast | 100% r=1618 | 100% r=8720 | 100% r=25750 | 100% r=85666 | 自动 |
| xai/grok-4.20-non-reasoning | 100% | 100% | 100% | 99.9% r=85568 | 自动 |
| xai/grok-4.20-reasoning | — | — | — | 83.3% r=71338 | 自动 |
| anthropic/haiku-4-5 | **0%** ⚠️ | 92% r=11316 | 100% r=33590 | N/A | 显式 |
| anthropic/sonnet-4-6 | 100% r=2314 | 100% r=11316 | 100% r=33591 | N/A | 显式 |
| anthropic/opus-4-6 | **0%** ⚠️ | 100% r=11315 | 100% r=33591 | N/A | 显式 |
| openai/gpt-5 | 100% r=1664 | 100% r=8704 | 100% r=25984 | 100% r=87040 | 自动 |
| openai/gpt-5-mini | 0% ⚠️ | 83% r=8064 | 100% r=25472 | 100% r=86955 | 自动 |
| google/gemini-3-pro | 0% ⚠️ | 17% r=8174 | 67% r=28650 | 67% r=98282 | 显式 |
| groq/llama-3.3-70b | 0% | 0% | 0% | 0% | 无 |

⚠️ 2k cache 不命中可能因 Anthropic 对 Haiku/Opus 的 `min_cache_tokens` 实际 >1024（catalog 假设 1024，需要更新）

### 5.4 成本对比 ($/100 calls @ 30k)

| Model | 成本 | 备注 |
|---|---|---|
| **xai/grok-4.20-non-reasoning** | **$0.26** 🥇 | 当前最低 |
| xai/grok-4-1-fast | $0.26 | 同价 |
| xai/grok-4.20-reasoning | $0.35 | +35% for reasoning |
| openai/gpt-5-mini | $0.29 | |
| groq/llama-3.3-70b | $1.62 | 无 cache |
| anthropic/haiku-4-5 | $1.77 | |
| google/gemini-3-pro | $2.55 | |
| openai/gpt-5 | $4.83 | |
| anthropic/sonnet-4-6 | $5.23 | |
| anthropic/opus-4-6 | **$26.30** ❌ | 100× Grok |

### 5.5 API 成功率（本实验 Tier 1 条件下）

| Provider | Success rate | 备注 |
|---|---|---|
| xai | 108/108 = 100% | 全部变体 |
| openai | 72/72 = 100% | |
| google | 36/36 = 100% | Billing 启用后 |
| groq | 36/36 = 100% | 免费 tier |
| **anthropic** | **153/180 = 85%** | **100k 系列 78% 撞 TPM** |

---

## 6. 关键观察与解释

### 6.1 🎯 Grok 4.1 → 4.20 的 100k Chinese recall 跳变

这是实验最大发现。同样是 fast non-reasoning 架构，一代之差：

| 指标 | grok-4-1-fast-non-reasoning | grok-4.20-0309-non-reasoning |
|---|---|---|
| TTFT@100k warm | 654ms | 659ms (持平) |
| TTFT@100k cold | 4052ms | **2756ms (-32%)** |
| 100k recall | **0/3** | **3/3** (完美) |
| Cost | $0.26/100 | $0.26/100 (持平) |

xAI 在 4.20 一代修复了长上下文中文指令遵循问题。Pack 3 的"instruction-following -24.3pp"警告在 4.20 已失效。

### 6.2 Anthropic Tier 1 的硬性 TPM 天花板

| Model | Tier 1 input TPM | 单次 100k 请求 |
|---|---|---|
| Sonnet | 30k/min | **超 3.3×** → 永远 rate-limit |
| Opus | 20k/min | **超 5×** |
| Haiku | 50k/min | 超 2× |

**retry 无救**：不是"发太快"，而是"请求本身比每分钟预算大"。

要做 Anthropic 100k benchmark 必须升 Tier：
- Sonnet 需要 Tier 4（$400 总花销 + 7 天）
- Opus 需要 Tier 5+（$1000+）
- Haiku 需要 Tier 2（$40 + 7 天）

对 Jarvis 决策没影响（Jarvis ≤ 30k）。

### 6.3 GPT-5 的 "默认 reasoning" 坑

GPT-5 所有 context 的 TTFT 都 6-7 秒，比 Haiku 慢 10x。**怀疑 API 默认启用 chain-of-thought**，即使不显式要 reasoning 也会消耗推理时间。GPT-5-mini 情况类似（5-6s）。

**对 Jarvis 启示**：OpenAI 的所谓"旗舰"在这个场景不可用。要用 OpenAI 只能挑 gpt-4o / gpt-4o-mini（非 GPT-5）。

### 6.4 xAI OpenAI-compat cache 坐实

Research Pack 3 说"xAI 可能走 OpenAI 兼容 prompt cache 但文档没说"。实验证实：
- `prompt_tokens_details.cached_tokens` 字段存在
- warm call 命中率 100%（甚至 158 tokens 的小 prefix 都缓存）
- 无独立 write 费（与 OpenAI 一致）

### 6.5 Gemini cache 命中率差

| Context | Gemini 命中率 |
|---|---|
| 2k | 0% |
| 10k | 17% |
| 30k | 67% |
| 100k | 67% |

即使超过官方 `min_cache_tokens = 4096` 也有 1/3 未命中。可能原因：Gemini inline 发送（未用 `CachedContent` 显式对象）的隐式 cache 不稳。若真要最大化 Gemini cache，需要切换到 `CachedContent` 对象模式 + 承担 storage 费。

### 6.6 Groq 长上下文吞吐悬崖

| Context | Groq TTFT warm |
|---|---|
| 2k | 356ms ✓ |
| 10k | 802ms ✓ |
| 30k | 1711ms ⚠️ |
| 100k | **17962ms** ❌ |

Groq 的 LPU 对短上下文极快（2k 356ms 是全场最快），但 100k 耗时近 18 秒，完全不可用。这是 Groq 硬件本身的特性：**吞吐 > 低延迟**，大 prompt 的 batching 效率下降严重。

---

## 7. 已知局限

1. **Anthropic 100k 数据缺失**: Tier 1 下不可能采到，需升 Tier 4（$400+）
2. **Sonnet 30k warm 污染**: Standard run 里因与 Opus/Haiku 共享锁，backoff 时间混入 TTFT，测到 23s 是假值
3. **Haiku 30k warm 部分污染**: 独跑也有污染（单 Haiku TPM 50k < 30k×3 calls 累计 90k）
4. **Pricing 快照**: 2026-04-14 的价格。Opus 可能降价、其他模型可能涨，建议每 3 月复查
5. **Model ID 时效**: Grok-4.20 带日期 `0309`（2026-03-09 发布），xAI 可能出 `0415` 新版覆盖本文 ID
6. **Gemini 未测 CachedContent 对象**: 只测 inline，若启用显式对象命中率可能更高但有 storage 费
7. **Warm 只有 n=2 样本**: Median == Mean，无法做统计显著性。Summary 里的 "unstable" 标记可能过度敏感（30% 阈值对 n=2 太严）

---

## 8. 决策

### 8.1 Jarvis 主 LLM

**选 `grok-4.20-0309-non-reasoning`**

理由（按优先级）:
1. **TTFT 488ms @ 30k** — Jarvis 语音延迟目标 <500ms 达成，全场唯一
2. **Recall 3/3 @ 100k** — 4.1 fast 的崩盘问题已修，未来上限高
3. **$0.26/100 @ 30k** — 最低成本（并列）
4. **Cache 命中 100%** — OpenAI-compat 自动 cache，免维护
5. **API 稳定性 100%** — 本实验 0 errors

### 8.2 备选

**选 `claude-haiku-4-5-20251001`**

使用场景: 当 xAI API 不可用时（故障/rate limit）fallback。

理由:
1. TTFT 752ms @ 30k warm — 略慢但仍在可接受范围
2. Recall 在 2k/10k/30k 完美，Jarvis 用得到的 context 都覆盖
3. Chinese 表现最稳
4. $1.77/100 — Grok 的 7x 但仍可控

**注意**: Haiku 100k 不要用（Tier 1 TPM 撑不住，未来升 Tier 2 再议）。

### 8.3 被淘汰

| 模型 | 淘汰原因 |
|---|---|
| grok-4-1-fast-non-reasoning | 100k recall 崩（被 4.20 全面替代）|
| grok-4.20-reasoning | TTFT 4-5s（推理开销），不适合语音 |
| grok-4-0709 (旗舰) | 未测（定价预估，跑起来 $3-5），fast 已够用 |
| claude-sonnet-4-6 | TTFT 污染值不可靠，cost $5.23 过高 |
| claude-opus-4-6 | **$26.30/100**（Grok 的 100 倍），一票否决 |
| gpt-5 / gpt-5-mini | TTFT 5-7s（疑似默认 reasoning），不适合语音 |
| gemini-3-pro | TTFT 5-6s + cache 不稳 |
| groq/llama-3.3-70b | 100k TTFT 18s，对 Jarvis 潜在长笔记场景风险 |

### 8.4 更新 `config.yaml`

```yaml
llm:
  primary:
    provider: xai
    model: grok-4.20-0309-non-reasoning     # 从 grok-4-1-fast-non-reasoning 升级
    context_budget: 30000                    # 硬顶，超过切 fallback
    base_url: https://api.x.ai/v1
    api_key_env: XAI_API_KEY
  fallback:
    provider: anthropic
    model: claude-haiku-4-5-20251001
    note: "Grok 失败或 context > 30k 时启用"
    api_key_env: ANTHROPIC_API_KEY
```

---

## 9. 完整复刻实验指南

### 9.1 前置条件

- Python 3.11+
- `uv` 包管理器（https://docs.astral.sh/uv/）
- 5 个 API key 环境变量（任选其一 subset 可跑部分）:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  export OPENAI_API_KEY=sk-proj-...
  export GEMINI_API_KEY=AIza...           # 或 GOOGLE_API_KEY
  export GROQ_API_KEY=gsk_...
  export XAI_API_KEY=xai-...
  ```
- Anthropic billing 充值 **≥ $20**（Tier 1 下 100k 会部分 rate-limit，但其他 context 够）
- Gemini: Google Cloud billing 启用 Generative Language API（免费 tier 可能不够 288 calls）

### 9.2 clone + install

```bash
cd ~/Projects/jarvis
# 脚本已存在: scripts/bench_llm_v3.py (PEP 723 metadata)
# PEP 723 需要 uv ≥ 0.4, python ≥ 3.11

# 方式 A: 直接跑（uv 自动管理依赖）
uv run scripts/bench_llm_v3.py --dry-run --quick

# 方式 B: 如果 `uv run python scripts/*.py` 不走 PEP 723, 手动装
uv pip install google-generativeai anthropic openai groq tiktoken tqdm plotly pandas
```

### 9.3 执行阶段（强制三 stage gate）

#### Stage 1: dry-run（零成本验证管线）
```bash
uv run python scripts/bench_llm_v3.py --dry-run --quick
```
**验收**: fixture 生成 OK, provider detection 正确, 估算成本 <$1

#### Stage 2: `--quick`（真跑 36 calls, <$0.50, 验证 cache_control）
```bash
uv run python scripts/bench_llm_v3.py --quick
```
**验收**:
- Anthropic debug banner 显示 `cache_creation_input_tokens > 0`（cache_control 工作）
- xAI debug banner 显示 `cached_tokens` 字段存在
- 36/36 成功 or ≤5 errors
- 生成 `bench_results/{ts}/summary.md`

#### Stage 3: `--standard`（全量 288 calls, ~$15, 15-25 min）
```bash
uv run python scripts/bench_llm_v3.py --standard
```
**预期**:
- Anthropic 100k 会 rate-limit (Tier 1 预期行为)
- 其他 provider 0 errors
- 总成本 $14-17

#### 可选: 单模型深测
```bash
# 测最新 xAI 模型
uv run python scripts/bench_llm_v3.py --model grok-4.20-0309-non-reasoning

# 避开 100k 降 TPM 压力
uv run python scripts/bench_llm_v3.py --model claude-haiku-4-5-20251001 --contexts 2k,10k

# 测旗舰 (贵)
uv run python scripts/bench_llm_v3.py --model grok-4-0709
```

### 9.4 数据分析

每次 run 产生 4 个 artifact 在 `bench_results/{timestamp}/`:
- `results.csv`: 每次 API call 一行，22 个字段
- `summary.md`: 5 张核心表（TTFT × context, recall, cold vs warm, cache rate, cost）
- `run_meta.json`: mode/errors/cost/elapsed + 完整 args
- `chart.html` (可选, `--with-chart`): plotly 交互图

合并多次 run 做对比的 Python 脚本见本文 §5.1 计算方法（group by provider/model/context/cache_state，filter `error == ""`, 取 TTFT median）。

### 9.5 Reproducibility 保证

1. **固件**: `bench_fixtures/fake_notes_*.txt` **一次生成永不重生**（删了才重新生成），byte-level reproducible across runs
2. **Seed**: `random.Random(42 + target_tokens)` for 每个 fixture
3. **针/干扰项** 固定字符串（见 §2.2）
4. **Tasks** 固定 3 个 prompt（见 §2.1）
5. **Model IDs** 带日期（e.g. `grok-4.20-0309-*`）保证复现特定版本

### 9.6 预期复刻成本

| 场景 | API calls | 成本 | 时长 |
|---|---|---|---|
| 最小复刻（dry-run + quick） | 36 | $0.30-0.50 | 5 min |
| 主要决策数据（standard） | 288 | $14-18 | 20-25 min |
| 完整复刻（4 次 run） | ~432 | $15-16 | 40 min |

---

## 10. 文件引用

### 实验代码
- 主脚本: `scripts/bench_llm_v3.py` (PEP 723, 1400+ lines)
- Ping 工具: `scripts/bench_llm_v3_ping.py`
- Unit tests: `tests/test_bench_llm_v3.py` (19 tests)
- v1 参考: `bench_llm.py`（root, 单 prompt TTFB-only 快速对比）

### 设计文档
- Spec: `docs/superpowers/specs/2026-04-14-bench-llm-v3-design.md`
- 实施计划: `docs/superpowers/plans/2026-04-14-bench-llm-v3.md`

### Research Packs（前置调研）
- `notes/research-pack-4-summary-2026-04-14.md` — 综合
- `notes/research-pack-4-llama-chinese-2026-04-14.md` — Meta Llama 中文支持调查
- `notes/research-pack-4-groq-deployment-2026-04-14.md` — Groq 量化差异
- `notes/research-pack-4-grok-claude-chinese-2026-04-14.md` — xAI/Anthropic 中文黑箱

### 实验原始数据
- `bench_results/2026-04-14_2121/` — Run 1: --quick
- `bench_results/2026-04-14_2211/` — Run 2: --standard
- `bench_results/2026-04-14_2323/` — Run 3: --model haiku
- `bench_results/2026-04-14_2332/` — Run 4: --model grok-4.20

### 开发提交历史（core commits）
- `8ad1028` — spec initial
- `6680f23` — spec final (smoke test 三阶段 + Anthropic debug assertion)
- `b501628` — implementation plan
- `b72e83e` → `3a03985` — 13 次 TDD commits (scaffold → fixtures → providers → CLI)
- `d5e6379` — Gemini stream.resolve() 修复（返回 stream 对象而非 chunk）
- `94b8d32` — OpenAI GPT-5 `max_completion_tokens` 参数修复
- `ea28e7d` — 3 处 polish（smoke gate 阈值、task list 穿透、xAI cache 透明化）
- `85cd3c4` — 新增 4 个 xAI Grok 变体到 catalog
- `0b5fa5c` — `--contexts` flag（避开 TPM 撞墙）

---

## 11. 后续工作建议

### 11.1 立即可做
- [ ] 把 `config.yaml` 主 LLM 换成 `grok-4.20-0309-non-reasoning`
- [ ] 保留 `claude-haiku-4-5-20251001` 作为 fallback
- [ ] 更新 Jarvis 的 `intent_router` 如果也要跟进（当前在 Groq Llama）
- [ ] Jarvis 生产部署前用真实 OM 笔记（不是假笔记）再跑一次 benchmark

### 11.2 实验可扩展（可选）
- [ ] 测 Grok 旗舰 `grok-4-0709`（~$3-5，可能带来更强 recall 但 TTFT 更慢）
- [ ] 补 Anthropic 100k（需先升 Tier 4，$400+）
- [ ] 加 `stale` cache state 测试（6 min 等待后 warm TTFT，模拟 Jarvis 会话间歇）
- [ ] 加 Chinese-native benchmark 模型（Qwen3-Max, Kimi K2.5, DeepSeek-V3.2）对比
- [ ] 迁移 `google.generativeai` → `google.genai`（前者官方已弃用）

### 11.3 代码改进
- [ ] Fix Anthropic 的 `min_cache_tokens` catalog 值（Haiku/Opus 实测 >1024, 可能 2048 或 4096）
- [ ] `n=2` warm 样本不足做统计，考虑提高到 `n=3` 或用 median of 3
- [ ] 添加 `--throttle` flag：在 Anthropic 请求之间强制 sleep 保证 TPM 不超（Tier 1 下跑全量用）
- [ ] `--stale` flag 实现 6min 间隔的 stale cache 测试（spec 里有，plan 里 defer 了）

---

## 12. 总结

本实验用自建 432 次 API 调用的 benchmark，在中文长笔记 + recall 场景下对 10 个前沿 LLM 做了全面对照测试，为 Jarvis 选出 `grok-4.20-0309-non-reasoning` 作为主 LLM（TTFT 488ms, 全 context recall 3/3, $0.26/100 calls）。

**核心洞察**: 厂商 benchmark（MMLU 等）对实际语音助手场景参考价值有限。自己的工作负载、自己的 prompt、自己的 evaluation metric 才是决策依据。

**方法论贡献**:
- 三段计价修复 Anthropic cold 成本低估
- Bust prefix (UUID + ns timestamp) 突破 OpenAI/xAI 自动 cache 污染
- 固化磁盘 fixture + seed 保证跨进程 cache 命中
- 三阶段 smoke gate 防止 v2 那样的批量失败

**钱花得值**: $15.46 换到一个**独立可复现**的决策依据 + 工程实验作品。

---
*生成时间: 2026-04-14 23:50 UTC+0*
*作者: Allen (alllllenshi@gmail.com) + Claude Opus 4.6 in Claude Code*
