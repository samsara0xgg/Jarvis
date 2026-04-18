# 调研 06: Grok-4.20 能否替代 Gemini/GPT 做 Observer/Reflector

*Generated: 2026-04-15 | Target: grok-4-fast-non-reasoning / grok-4-0709 / grok-4-latest*
*Context: Mastra Observational Memory 需要 LLM 严格输出 XML tag (`<observations>...`)，官方说 Claude 不行、默认 Gemini 2.5 Flash。评估能否换成 Grok 统一 provider。*

---

## 执行摘要

- **Q1 (function calling / structured output): 可用** — Grok-4 全家支持 JSON Schema + tools，OpenAI-SDK 兼容。
- **Q2 (XML tag 严格输出): 数据不足 / 谨慎用** — 无第三方 XML-drift benchmark；xAI 自己文档只教 JSON 不教 XML。
- **Q3 (中文 extraction): 不可用** — `grok-4-1-fast-non-reasoning` ReLE 47.6% 垫底，指令遵从暴跌 -24.3pp。
- **Q4 (长 context 30k–50k 输出质量): 数据不足** — 2M 窗口是营销数，无 RULER/LongBench/Chroma 独立测评。
- **Q5 (Mastra 集成): 可用** — Mastra 一等公民 provider，25 个 Grok 变体，走 `@ai-sdk/xai`。

**综合结论：不可用（用于 Observer/Reflector）。**
主要死因 = Q3 中文 extraction 能力塌方 + Q2 XML 模式没有证据支撑。如果坚持用 Grok，必须(1) 改走 JSON Schema 而非 XML tag、(2) 换掉 `fast-non-reasoning` 变体改用 `grok-4` 或 reasoning 变体、(3) 自建 30-50k 中文 fixture 回归测试。对 Jarvis 这种主打中文私人管家的场景，保留 Gemini 2.5 Flash 做 Observer 是更稳的选择。

---

## Q1. Function Calling / Structured Output 支持

| 结论 | 来源 |
|---|---|
| xAI 官方支持 JSON Schema 结构化输出，Pydantic/Zod → JSON，strict mode 保证 schema 符合，`parse()` / `response_format` 均可用，Grok-4 全家支持 structured output + tool calling 组合 | https://docs.x.ai/docs/guides/structured-outputs |
| Function calling 原生支持 — `tools` array + JSON-schema params，`tool_choice` (auto/required/none/specific)，默认并行调用，OpenAI SDK 兼容 `base_url=https://api.x.ai/v1` | https://docs.x.ai/docs/guides/function-calling |
| `grok-4-fast-non-reasoning` 在多个第三方网关被明确列出 "function calling" / "tool calling" 能力 | https://inworld.ai/models/xai-grok-4-fast-non-reasoning ; https://www.typingmind.com/guide/xai/grok-4-fast-non-reasoning |
| ⚠️ 历史 bug：2025-08 `grok-4-0709` 在 `response_format=json_schema` 下返回空 `content` (completion_tokens=0, reasoning_tokens 吃光预算)；~2025-08-17 修复。Grok-3 未受影响 | https://github.com/prism-php/prism/issues/545 |
| ⚠️ 已知 tool_call bug：Grok 有时会把 `tool_calls` 里 JSON arg HTML-entity-encode (`&quot;` 等)，`JSON.parse` 会炸 | https://github.com/openclaw/openclaw/issues/15612 |

**判定：可用。** Grok-4 family 有一等公民 `tools` + `response_format` JSON-schema (strict)。`grok-4-fast-non-reasoning` 规避了 reasoning-token 耗尽空返问题。

---

## Q2. XML Tag 严格输出稳定性

| 结论 | 来源 |
|---|---|
| IFEval 类指令遵从榜（测 format 约束）Qwen/Kimi/GPT/Claude/DeepSeek 都有分数，**Grok-4 到 2026-04 没进 top-15**，聚合榜无公开 IFEval 分数 | https://benchlm.ai/benchmarks/ifeval |
| "XML-tagged thinking" 是 Anthropic 的 prompt convention；xAI 自己的 extraction 文档**只教 JSON schema**，没有任何 XML tag 抽取指南 | https://grokipedia.com/page/XML-tagged_thinking ; https://docs.x.ai/docs/guides/structured-outputs |
| LLM 普遍在 pressure 下会漏闭合 tag（社区共识，非 Grok 专属）；r/LocalLLaMA / r/grok 没搜到 Grok-specific XML-drift 吐槽 | https://www.reddit.com/r/LocalLLaMA/comments/1hspd85/missing_closing_tags_when_llm_outputs_xml/ |
| 16x 独立 eval：Grok-4 reasoning 变体烧 5.6k reasoning tokens / 150s，历史上 reasoning 超支时会返回空 content — 无论 XML 还是 JSON 都可能空转 | https://eval.16x.engineer/blog/grok-4-evaluation-results |
| 无公开数据直接测 `grok-4-fast-non-reasoning` 或 `grok-4-0709` 的 XML tag drop/reorder 率 | 无公开数据 |

**判定：数据不足，谨慎用。** 如果 Mastra Observer 必须 `<observations>` XML 标签，建议包一层 JSON schema `{observations: string}` 让 schema 兜底，不要裸依赖 Grok 的 XML 自律。

---

## Q3. 中文 Extraction 真实表现

| 结论 | 来源 |
|---|---|
| **`grok-4-1-fast-non-reasoning` ReLE Chinese LLM benchmark 47.6%**（~15k 题），**垫底于新模型阵营**。"语言与指令遵从" **44.0%**（-24.3pp vs grok-3-mini）。金融 51.5% / 法律 50.7% / 医疗 51.4% 全部徘徊在"不可用阈值" | https://www.itsolotime.com/archives/14607 (ReLE, data: https://github.com/jeinlee1991/chinese-llm-benchmark) |
| 同期对比：gemini-3-pro-preview 72.5% / DeepSeek-V3.2-Think 70.1% / gpt-5.1-medium 69.3% / hunyuan-turbos 65.9% | 同上 |
| Grok 4（full）在 DataLearner 标 "支持中文"，但 **C-Eval / CMMLU / CLUE 均无条目**；Grok-4 card 只报 MMLU-Pro / GPQA / AIME / SWE-bench（全英文+数学+代码） | https://cevalbenchmark.com/static/leaderboard.html ; https://llmlearner.com/ai-models/pretrained-models/grok-4 |

**判定：不可用（用于中文 extraction）。** `fast-non-reasoning` 变体的中文指令遵从直接塌方，Jarvis 中文 entity/intent 抽取场景禁用。若必须 Grok 系，改用 `grok-4` 或 reasoning 变体，且需要自测。

---

## Q4. 长 Context (30k–50k) 输出质量

| 结论 | 来源 |
|---|---|
| Awesome Agents 长 context 榜 (2026-02)：Grok 4 Fast 标 2M 窗口，**"limited independent benchmarks available"**；无 MRCR/RULER/LongBench-v2 分数；rank #5 是名义值非证据。备注 "effective capacity 通常是广告上限的 60-70%" | https://awesomeagents.ai/leaderboards/long-context-benchmarks-leaderboard/ |
| Chroma "Context Rot" 报告测了 18 个 frontier model（Claude Sonnet 4 / GPT-4.1 / Qwen3-32B / Gemini 2.5 Flash），**Grok 未在测试集内** | https://research.trychroma.com/context-rot |
| NVIDIA RULER repo / NoLiMa (arXiv 2502.05167)：**无 Grok-4 公开分数** | https://github.com/NVIDIA/RULER ; https://arxiv.org/abs/2502.05167 |

**判定：数据不足。** 2M context 是 xAI 的自家营销数，没有第三方压力测试。如果 Observer 要处理 30-50k 的对话历史，必须自己搭 fixture 验证后再上。

---

## Q5. Mastra / LangChain 集成案例

| 结论 | 来源 |
|---|---|
| Mastra 一等公民 xAI provider，25 个模型包括 `xai/grok-4-fast-non-reasoning` (2M context, $0.20/$0.50 per 1M)、`xai/grok-4`、`xai/grok-4.20-*`。支持 tools / `reasoningEffort` / `searchParameters` / custom headers。`XAI_API_KEY` 环境变量，底层 `@ai-sdk/xai` | https://mastra.ai/models/providers/xai ; https://github.com/mastra-ai/mastra/blob/main/docs/src/content/en/models/providers/xai.mdx |
| Vercel AI SDK `@ai-sdk/xai` (v6) 做底层，Grok feature parity 与 OpenAI/Anthropic 对齐 | https://ai-sdk.dev/providers/ai-sdk-providers/xai |
| 无公开 case studies 报告 Mastra+Grok 生产事故；GitHub issue 无阻塞项 | 无公开数据 |

**判定：可用。** 集成层没有坑。

---

## 行动建议

1. **不换。** Observer/Reflector 继续用 Gemini 2.5 Flash。主要因为 Q3 — `grok-4-fast-non-reasoning` 中文指令遵从 44%，直接排除。
2. 如果想省成本统一 provider，**降级方案**：把 Observer 换成本地 Qwen2.5-7B / DeepSeek-V3 API（中文强 + JSON schema 能力成熟），仍然比 Grok 稳。
3. 若未来 xAI 出 `grok-4.20-reasoning` 中文补强版，再重测 ReLE + 自建 30k 中文 fixture。
4. Mastra Observer 若支持把 XML 改造成 JSON schema 输出，**强烈建议先改 schema**（任何 provider 都更稳），再谈换模型。

---

## Sources (汇总 12 条)

1. [xAI Structured Outputs docs](https://docs.x.ai/docs/guides/structured-outputs) — 官方 JSON schema + strict mode
2. [xAI Function Calling docs](https://docs.x.ai/docs/guides/function-calling) — tools/tool_choice 规范
3. [Inworld — grok-4-fast-non-reasoning](https://inworld.ai/models/xai-grok-4-fast-non-reasoning) — 第三方能力声明
4. [TypingMind grok-4-fast-non-reasoning](https://www.typingmind.com/guide/xai/grok-4-fast-non-reasoning)
5. [Prism PHP issue #545](https://github.com/prism-php/prism/issues/545) — grok-4-0709 json_schema 空返 bug (2025-08)
6. [openclaw #15612](https://github.com/openclaw/openclaw/issues/15612) — tool_calls HTML-entity-encode bug
7. [BenchLM IFEval leaderboard](https://benchlm.ai/benchmarks/ifeval) — Grok-4 未上榜
8. [16x Grok-4 eval](https://eval.16x.engineer/blog/grok-4-evaluation-results) — reasoning 耗尽空返风险
9. [itsolotime ReLE 2026-04](https://www.itsolotime.com/archives/14607) — **关键：中文 47.6% 垫底**
10. [chinese-llm-benchmark](https://github.com/jeinlee1991/chinese-llm-benchmark) — 原始数据
11. [Awesome Agents long-context board](https://awesomeagents.ai/leaderboards/long-context-benchmarks-leaderboard/) — "limited independent benchmarks"
12. [Mastra xAI provider](https://mastra.ai/models/providers/xai) — 25 变体一等公民

## Methodology

Dispatched 2 parallel research agents (exa web_search + crawling) covering 5 sub-questions across xAI docs / IFEval / ReLE / RULER / Chroma / Mastra / LangChain / GitHub / Reddit / zhihu 范围。共检索 ~20 URLs，深读 ~8 条。所有论断带可点链接，xAI 营销口径不计入证据。
