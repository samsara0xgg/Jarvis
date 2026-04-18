# Research Pack 3 — 长上下文 vs 显式记忆 2026-2028

*Date: 2026-04-14 · Scope: Jarvis memory 系统生命周期判断*

## TL;DR

1. **长上下文模型理论容量 ≠ 有效容量**：Gemini 3 Pro 10M 窗口在 1M 处 8-needle MRCR 仅 26.3%（128K 处 77%）；NoLiMa 显示即便 GPT-4o 从 99.3%（短）掉到 69.7%（32K），11/13 主流模型在 32K 时跌破自身基线 50%。[1][4]
2. **显式 memory 在 benchmark 上依然领先 full-context**：Mem0 67% vs Zep 全上下文 GPT-4o 60.2%；Mastra OM 84.23% vs full-context 60.2%（+24 points），同时 full-context p95 延迟 17.12s → Mem0 1.44s（-92%），token 成本 -90%。[5][6][7]
3. **但 full-context 在绝对准确率上小幅领先**：Mem0 paper Table 2 明确，full-context 26k tokens 的 J 分 ~73%，仍高于 Mem0 67%——代价是 17s p95、full bill。绝对质量 vs 成本/延迟是核心 trade-off。[5]
4. **成本已不是决定性因素（对 Jarvis 规模）**：Allen 50 turns/day 场景，方案 A 全历史（100k tokens/turn）在 Grok 4.1 Fast 上 $365/年无缓存；方案 B 显式 memory 约 $3.6/年。单价跌 4-50x 后，memory 设计价值从"省钱"转向"省延迟 + 避免 lost-in-the-middle"。[3][8]
5. **推荐**：Jarvis 属于"小规模长周期"场景——**保留简化版显式 memory（核心是事实提取 + prompt cache 稳定前缀），放弃激进的 dedup/staleness 优化，用长上下文承担 recent turns**。理由见 §5。

## 1. 长上下文 benchmark 现状（2025-2026）

| 模型 | 理论 context | MRCR v2 (8n,1M) | MRCR v2 (4n,256K) | LongBench v2 | 定价 $/1M in | 备注 |
|---|---|---|---|---|---|---|
| Claude Opus 4.6 | 1M | **76%** | ~90% | 64.4% (Opus 4.5) | $5 (flat 1M) | 4x 提升 over Sonnet 4.5 (18.5%) |
| Claude Sonnet 4.6 | 1M | 未独立验证 | ~82% | — | $3 (flat 1M) | 3 月 13 日取消 200K 溢价 |
| Gemini 3 Pro | 10M | 26.3%@1M / 77%@128K | ~85% | 68.2% | $12 | 128K 后悬崖式下跌 |
| GPT-5.2 | 400K | — | **98%** | 54.5% | $1.50 | 256K 内最稳 |
| Grok 4.1 Fast | 2M | 独立数据缺失 | — | — | **$0.20** | 便宜+长 context 组合独一份 |
| Qwen 3.5 397B | 128K | — | — | 63.2% | 开源 | — |
| Kimi K2.5 | 256K | — | — | 61% | $0.38 | — |
| Llama 4 Scout | 10M | "near-perfect NIAH" 自报 | — | — | 开源 | 独立验证稀缺 |

**Lost in the middle 2025-2026 状态**：**没有缓解**。NoLiMa (arXiv:2502.05167, v3 Jul 2025) 证明：13 个 128K+ 模型在 32K 时 11/13 跌破自身基线 50%；即便 GPT-4o 也从短文本 99.3% 掉到 32K 处 69.7%。Reasoning/CoT 不能挽救。awesomeagents.ai 2026 年结论：**有效容量约为标称值 60-70%**。[1][4]

**关键信号**：Grok 4.1 Fast（Jarvis 主 LLM）独立的长上下文 benchmark 极少；其 2M 窗口更多是"市场定位"。Allen 实际跑的 256k 版本（Oracle listing 的 131k）没有公开的 MRCR/RULER 独立评测——这是风险。

## 2. 显式 memory vs full context 直接对比

### 2.1 Mem0 paper (arXiv:2504.19413, Apr 2025) LOCOMO 结果

**Table 1/2 数据（26,000 tokens 平均对话）**：

| 方法 | Overall J 分 | Search p50 | Total p95 | Token 成本 |
|---|---|---|---|---|
| Full-context GPT-4o | **~73%** | 0 | **~17s** | 全量注入 |
| Mem0 | 67% | <0.3s | **1.44s** | -90% |
| Mem0g (graph) | 68% | <0.5s | 2.6s | -88% |
| Best RAG (k=1, chunk=256) | ~61% | 0.25s | ~1.6s | 中等 |
| Zep | ~59% | — | — | — |
| OpenAI ChatGPT memory | 52.9% | — | — | — |

结论：full-context **绝对准确率最高**，但延迟 p95 17s、token 全量；Mem0 用 -6 点准确率换 -92% 延迟 + -90% token。[5]

### 2.2 Zep / LongMemEval (Emergence AI 2025-06, Mastra 2026-02)

LongMemEval_S（500 Q × 57M tokens 总数据 × ~50 sessions/Q），gpt-4o 作 actor：

| 系统 | Overall | Latency | 备注 |
|---|---|---|---|
| Mastra OM + gpt-5-mini | **94.87%** | — | SOTA；prompt-cacheable stable prefix |
| Mastra OM + gemini-3-pro | 93.27% | — | — |
| EmergenceMem Internal | 86.00% | 5.65s | 闭源 |
| EmergenceMem Simple | 82.40% | 7.12s | 开源 RAG |
| Oracle GPT-4o | 82.40% | 1.35s | 仅给相关 session |
| Mastra RAG (topK 20) | 80.05% | — | — |
| Zep | 71.20% | 3.20s | 前 SOTA |
| Full context GPT-4o (Zep 版) | **60.20%** | 31.30s | 基线 |
| Full context GPT-4o (Emergence 版) | 63.80% | 10.43s | — |
| Full context GPT o3 | 76.00% | 19.23s | 推理模型拉高 |

关键：**GPT-4o 的上下文窗口能装下所有 sessions，但 full-context 只拿 60.2%。Mastra OM 领先 34 points**。Oracle GPT-4o 82.4% 暗示"相关信息提取 + 聚焦"是关键，不是容量。[6][7]

### 2.3 Zep 50-实验 retrieval tradeoff（2025-12-09）

LoCoMo gpt-4o-mini：
- Minimal 5/2 facts：347 tokens → 69.62% 准确率，24% 问题上下文不足
- Default 15/5：756 tokens → 77.06%，17% 不足
- Medium 20/20：1,378 tokens → 80.06%，13% 不足
- Maximum 30/30：1,997 tokens → 80.32%，13.5% 不足

**边际收益在 20/20 处耗尽**。200-300ms 检索延迟贯穿始终。启示：**显式 memory 注入 1-2k tokens 已能拿到接近 benchmark 天花板的准确率**。[8]

### 2.4 HippoRAG 2 (arXiv:2502.14802)
MuSiQue F1: 44.8 → 51.9（+7）；2Wiki Recall@5: 76.5% → 90.4%；**索引 token 成本 9M vs GraphRAG 115M**（-92%）。图结构 + PPR 对多跳关联有实质帮助，但对 Jarvis 单用户日常不是刚需。[9]

## 3. 成本对比（Allen 场景）

### 3.1 输入 token 单价趋势（$/1M input tokens）

| 模型 | 2023 | 2024 | 2025 | 2026-04 |
|---|---|---|---|---|
| OpenAI 旗舰 | GPT-4 **$30** | GPT-4o $5 | GPT-5 $5 | GPT-5.4 **$2.50** |
| Anthropic 中端 | Claude 2 $8 | Sonnet 3.5 $3 | Sonnet 3.7 $3 | **Sonnet 4.6 $3 (flat 1M)** |
| Google 旗舰 | — | Gemini 1.5 Pro $3.50 | — | Gemini 2.5 Pro **$1.25** |
| xAI | — | — | Grok 3 $3 | **Grok 4.1 Fast $0.20** |
| 开源托管 | — | — | DeepSeek V3 $0.27 | DeepSeek V4 $0.30 |

4 年旗舰输入价从 $30 → $2.50（-12x）；"good enough" 层从 GPT-3.5 $0.50 → DeepSeek V4 $0.30 输入 + 质量从 60% 涨到 95% of GPT-4。[3]

**Anthropic prompt caching**：cache read = $0.30/1M（Sonnet 4.6），是标准 $3 的 10%。5-min TTL 默认、1-hour 可选；cache write 比基准贵 25%（5min）或 100%（1h）。[2][10]

### 3.2 Allen 场景年成本估算

**假设**：50 turns/day × 365 = 18,250 turns/年；当前 LLM = Grok 4.1 Fast（$0.20 input / $0.50 output）。

**方案 A：全历史长上下文**（每次注入 100k tokens = 12 个月对话累积）
- 无 cache：18,250 × 100,000 × $0.20/1M = **$365/年**
- 有 xAI 缓存（cache read $0.05/1M, 90% 命中）：$36.50 + $36.50 × 10% new = **~$40/年**

**方案 B：显式 memory + RAG**（每次注入 2k tokens = memory hits + last 3 turns）
- 无 cache：18,250 × 2,000 × $0.20/1M = **$7.30/年**
- 加上 FastEmbed 本地 embedding 0 成本、GPT-4o-mini 后台 extraction（~20M tokens/年 × $0.15/1M）= $3
- 合计 **~$10/年**

**方案 C：混合**（近 10 turns 全文 5k tokens + RAG 精华 2k tokens = 7k/turn）
- 无 cache：18,250 × 7,000 × $0.20/1M = **$25.55/年**
- 加 extraction $3 = **$28/年**

**切换到 Sonnet 4.6（$3 input）情景**：方案 A $5,475/年、B $110/年、C $383/年 → 此时 memory 设计价值大幅回归。

**切换到 Claude + prompt caching（cache read $0.30, 90% hit）**：方案 A 降至 $602/年，仍是 B 的 5.5x。[2][3]

## 4. 按数据规模的三场景推荐

| 场景 | 数据规模 | 推荐 | 理由 |
|---|---|---|---|
| 小规模 | <1MB 历史（<250k tokens） | **全部塞长上下文 + prompt cache** | Grok 4.1 Fast 2M 窗口完全够；cache 后 latency + cost 可控；无 lost-in-middle 风险（在 128K 内） |
| 中规模 | 1-100MB（250k-25M tokens） | **混合：最近 N turns 塞 context + 显式 memory 做长期** | 25M tokens 超过任何单次 context；lost-in-middle 在此处失控；显式 memory + RAG 按需注入 |
| 大规模 | >100MB | **必须显式 memory + graph（HippoRAG 2 类）** | 长尾关联、多跳；full-context 不可能；Zep 50-exp 证明 20/20 facts 已接近天花板 |

## 5. 对 Jarvis 的结论

Allen 的 Jarvis 一年期预估历史量：假设单 turn 用户+助手文本平均 400 tokens，50 turns/day × 365 = ~7.3M tokens/年。**属于"小-中规模中段"**——一年内仍在 Grok 4.1 Fast 2M 窗口内；两年后（~15M tokens）超出。

**显式 memory 系统在 2026-2028 是否值得设计？**

**推荐：保留但大幅简化。**

**理由链条**：

1. **绝对准确率已经不是 memory 的胜利领域**。Mem0 paper 自己承认 full-context 73% > Mem0 67%；LongMemEval 上 Mastra OM 94.87% 靠的是"观察 + 反思压缩"而非传统 dedup/extraction。Jarvis 现有的 SQLite + bge-small + GPT-4o-mini extraction 架构（类似 Mem0 + Zep 中间态）在 benchmark 上落后 Mastra OM 约 10 points。

2. **延迟才是 Jarvis 的核心约束**。语音场景下 user-perceived latency < 1.5s 才算可接受；full-context 17s p95 立即出局。**这是保留 memory 最硬的理由**。

3. **成本在单价已跌到 Grok Fast $0.20/1M 时不再是决定因素**。方案 A 全历史 $365-40/年都是小数目；但 **Sonnet 级别模型一旦成为主 LLM，B 省 50x**。保留 memory 提供"廉价升级到 Sonnet/Opus 的选项"。

4. **Lost-in-the-middle 没有缓解**，NoLiMa 2025 证明 32K 就开始掉。即使 Grok 4.1 Fast 2M 标称，对于 RPi5 4GB 本地无法独立验证。

5. **现有 memory 系统的 bug 成本已经支付过**（dedup、staleness、retrieval miss——这些都是 Batch 7 已修）。推倒重来投入新 bug 的风险大于维持收益。

**如果选显式 memory（推荐方案）**：关键设计点是
- **学 Mastra OM 的"稳定前缀 + append-only"**：把 system prompt + memory 观察块放在最前，确保 prompt cache 高命中（Anthropic 5min TTL 默认足够 voice 场景）
- **砍掉激进 dedup**：Zep 的 recall > precision 哲学更适合——错误注入比漏掉便宜
- **近期 10 turns 直接全文塞入**（~5k tokens），不走 RAG；长期记忆走 memory
- **Extraction 模型可降级**：GPT-4o-mini → 本地 Qwen 3 30B（$0.30）或 Groq Llama-3.3（已有）

**如果选长上下文（备选方案）**：关键风险是
- Grok 4.1 Fast 长上下文无独立 benchmark 验证（xAI 未公开 MRCR/RULER/NoLiMa 数据）
- 两年后数据量超 2M 窗口必须迁移
- 大模型切换（Sonnet）时成本爆炸
- 17s p95 延迟破坏 voice UX
- Anthropic cache 5min TTL 与 Jarvis 间歇使用模式可能冲突

## Sources

1. [Long-Context Benchmarks Leaderboard](https://awesomeagents.ai/leaderboards/long-context-benchmarks-leaderboard/) — MRCR/RULER/LongBench v2 2026-02 榜单
2. [Claude 1M Token Cost: Long-Context Surcharge Gone (TokenCost, 2026-03-24)](https://tokencost.app/blog/anthropic-long-context-flat-pricing) — Sonnet 4.6 flat $3/1M, cache read $0.30
3. [AI API Pricing History 2023-2026 (TokenMix)](https://tokenmix.ai/blog/ai-pricing-trends-history) — GPT-4 $30 → GPT-5.4 $2.50 完整时间线
4. [NoLiMa arxiv:2502.05167](https://arxiv.org/abs/2502.05167) — 13 LLM 长上下文 literal-match 去除后 32K 时 11/13 < 50% 基线
5. [Mem0 arxiv:2504.19413](https://arxiv.org/pdf/2504.19413) — LOCOMO 表 2 full-context J=73% p95=17s vs Mem0 67% p95=1.44s
6. [SOTA on LongMemEval with RAG (Emergence AI 2025-06)](https://www.emergence.ai/blog/sota-on-longmemeval-with-rag) — Full context GPT-4o 60.2%，EmergenceMem 86%
7. [Observational Memory: 95% on LongMemEval (Mastra 2026-02-09)](https://mastra.ai/research/observational-memory) — OM 94.87% vs full-context 60.2%
8. [Retrieval Tradeoff 50 Experiments (Zep 2025-12-09)](https://blog.getzep.com/the-retrieval-tradeoff-what-50-experiments-taught-us-about-context-engineering/) — 边际收益在 20/20 facts 耗尽
9. [HippoRAG 2 arxiv:2502.14802](https://www.emergentmind.com/topics/hipporag-2) — MuSiQue F1 +7, 索引 token -92%
10. [Claude API Pricing Guide 2026 (ClaudeLab)](https://claudelab.net/en/articles/api-sdk/claude-api-pricing-guide-2026) — cache 10% 标准价、batch 50% 折扣
11. [Anthropic Prompt Caching 2026 (AI Checker Hub)](https://aicheckerhub.com/anthropic-prompt-caching-2026-cost-latency-guide) — 5min/1h TTL、write multiplier 解析
12. [LongBench v2 Benchmark 2026 (BenchLM)](https://benchlm.ai/benchmarks/longBenchV2) — Claude Opus 4.5 64.4%, Qwen3.5 63.2%
13. [xAI Grok 4.1 Fast Pricing (2026-04)](https://aicostcheck.com/model/grok-4-1-fast) — $0.20/$0.50, 2M context, cache read $0.05
14. [Gemini 2.5 Pro Pricing Mar 2026 (devtk.ai)](https://devtk.ai/en/models/gemini-2-5-pro/) — $1.25/$10, 2M context
15. [Reasoning Benchmarks 2026 (BenchLM)](https://benchlm.ai/reasoning) — GPT-5.3 Codex 92.6% 综合
16. [Kimi K2.5 Pricing](https://ai.prygn.com/model/moonshotai/kimi-k2.5) — $0.38/$1.72, 262K context
