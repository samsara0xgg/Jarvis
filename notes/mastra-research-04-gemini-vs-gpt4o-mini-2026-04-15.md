# 调研 04：Gemini 2.5 Flash vs GPT-4o-mini（中文信息抽取）

*生成日期: 2026-04-15 · 来源数: 19 · Confidence: Medium-Low（直接对比数据稀缺）*

## Executive Summary

- **没有找到**任一机构发布 Gemini 2.5 Flash vs GPT-4o-mini 在中文 structured extraction 上的直接 head-to-head benchmark。所有"对比"站点（docsbot/llm-stats/aimodels.fyi）引用的都是英文 MMLU/GPQA，不适用于选型。
- **最接近的中文权威榜单**是 SuperCLUE 2025 年度综合榜，但它不区分 Gemini 2.5 Flash 和 Pro，且侧重推理/写作而非信息抽取。
- **价格是确定差异**：GPT-4o-mini 比 Gemini 2.5 Flash **便宜 2~4 倍**。输入 $0.15 vs $0.30，输出 $0.60 vs $2.50（每百万 token，2026-03 数据）。
- **两者都存在可复现的 function call / JSON 失败模式**，但触发条件不同（见 Q5）。
- **Mastra 默认用 Gemini 2.5 Flash 做 Observer/Reflector**，但这是基于英文 LongMemEval，**不是中文验证**。
- **倾向建议**：若主要考虑成本 + 抽取可靠性，且你的现有 prompt 已针对 GPT-4o-mini 调优过、没出过中文问题——继续用 GPT-4o-mini。没有足够证据证明 Gemini 2.5 Flash 在中文 observation 抽取上更好。**Confidence: Low-Medium**。

---

## Q1. 中文 structured extraction 准确率对比

**无公开可靠数据。**

具体说明：
- Papers with Code / HuggingFace 上没有两模型在 CLUE/C-Eval/CMMLU 的 structured extraction 子任务 head-to-head 结果。
- docsbot.ai 和 llm-stats.com 的"Gemini 2.5 Flash vs GPT-4o-mini"对比页，引用的是 MMLU / GPQA / HumanEval（英文通识、编程），**完全没有中文基准**。来源：[docsbot comparison](https://docsbot.ai/models/compare/gemini-2-5-flash/gpt-4o-mini)、[llm-stats](https://llm-stats.com/models/compare/gemini-2.5-flash-vs-gpt-4o-mini-2024-07-18)
- SuperCLUE 2025 年度报告列出了全球模型中文综合能力排名，**头部位置**由 Claude-Opus-4.5-Reasoning（68.25）、Gemini-3-Pro-Preview（65.59）、OpenAI 模型（65.xx）占据。但**榜单不包含 Gemini 2.5 Flash 或 GPT-4o-mini 单独条目**——只测了旗舰模型。来源：[SuperCLUE 2025 Report](https://www.cluebenchmarks.com/superclue_2025_en)、[SuperCLUE 公众号](https://mp.weixin.qq.com/s/w9Jyt5e3BR-lvaIMEjTAew)
- Google Gemini 2.5 技术报告（[arxiv 2507.06261](https://arxiv.org/pdf/2507.06261)）报告多语言性能但不细分中文 extraction 任务。
- OpenAI GPT-4o-mini 官方发布博客（[OpenAI 2024-07-18](https://openai.com/index/gpt-4o-mini-advancing-cost-efficient-intelligence/)）用 MGSM 79.7% 声明"strong multilingual"，但不报告中文单独成绩。

**结论：决策不能基于公开 benchmark。必须自己跑小样本测试。**

---

## Q2. 中文 NER 对比

**无直接对比可靠数据。**

- DynamicNER（[arxiv 2409.11022](https://arxiv.org/html/2409.11022v4)，EMNLP 2025）是最新多语言 LLM-NER benchmark，包含中文，但论文里测的是 GPT-4/Claude/Gemini-Pro 等旗舰款，**没有 Gemini 2.5 Flash 和 GPT-4o-mini 的对比数据**。
- CLiB 中文大模型榜单（[牛客网综述](https://www.nowcoder.com/discuss/776560061471563776)）侧重通用能力和 agent 任务，不含 NER 子任务。
- 学术圈公开数据里，两个"mini/flash"档位的中文 NER 直接对比**不存在**。

**结论：若此项对选型至关重要，需要自建 30~100 条中文 observation 的小样本集，本地对比。**

---

## Q3. 指令跟随 / function call / 结构化输出稳定性

### GPT-4o-mini 已知问题（2024-2026）

- **"Structured Outputs not reliable with GPT-4o-mini and GPT-4o"** — OpenAI 官方社区长贴，多名开发者报告 JSON schema 在复杂场景下被忽略。来源：[OpenAI Community](https://community.openai.com/t/structured-outputs-not-reliable-with-gpt-4o-mini-and-gpt-4o/918735)
- **"Function JSON Schema is still ignored by GPT-4, 4o, and 4o-mini"**（2024-08，复现案例）。来源：[OpenAI Community](https://community.openai.com/t/function-json-schema-is-still-ignored-by-gpt-4-4o-and-4o-mini-when-calling-tools/895368)
- **复杂 function call 结果解析失败**（2024-08）。来源：[OpenAI Community](https://community.openai.com/t/gpt-4o-mini-cant-parsing-complicate-function-callings-result-but-user-message-parsing-is-good/916805)
- **响应格式 + tool descriptions 冲突**（2024-12）。来源：[OpenAI Community](https://community.openai.com/t/unexpected-tool-call-behavior-with-response-format-and-tool-descriptions-in-gpt-4o-mini-api/1050977)
- OpenAI 2024-08 起提供 Structured Outputs (strict JSON schema) 模式，可**强约束**输出格式（但降低部分能力、也有 bug 报告）。

### Gemini 2.5 Flash 已知问题（2025-2026）

- **`MALFORMED_FUNCTION_CALL` finish_reason**（2026-02，仍 open）：streaming + tool definitions + dynamic thinking 组合下，function call 输出格式损坏。来源：[googleapis/python-genai #2081](https://github.com/googleapis/python-genai/issues/2081)
- **JSON mode + function calling 冲突**（2026-02）：agno 框架报告需要专门 workaround。来源：[agno-agi/agno #6655](https://github.com/agno-agi/agno/issues/6655)
- **Flash-Lite 生成 Python 语法作为 tool call 而非 JSON**（2025-11）。来源：[genkit-ai/genkit #3772](https://github.com/genkit-ai/genkit/issues/3772)
- Native audio 模型 function calling 失效（2025-05）。来源：[python-genai #843](https://github.com/googleapis/python-genai/issues/843)

**两边都有确实存在的 edge-case bug**。GPT-4o-mini 的问题更陈旧、社区解决方案更成熟；Gemini 2.5 Flash 的问题更新鲜、跟 thinking/streaming 强耦合。

---

## Q4. 成本对比（每百万 token，USD，2026-03 数据）

| 模型 | 输入 | 输出 | 上下文 |
|---|---|---|---|
| **GPT-4o-mini** | $0.15 | $0.60 | 128K |
| **Gemini 2.5 Flash** | $0.30 | $2.50 | 1M |

**差异**：GPT-4o-mini 输入便宜 2×，输出便宜 **~4.2×**。对 Observer 场景（输入为对话片段，输出为结构化 JSON observation），输出 token 量通常占主导，**GPT-4o-mini 总成本优势更大**。

来源：
- Gemini 2.5 Flash — [devtk.ai](https://devtk.ai/en/models/gemini-2-5-flash/)、[aicostcheck.com](https://aicostcheck.com/model/gemini-2-5-flash)、[pricepertoken.com](https://pricepertoken.com/pricing-page/model/google-gemini-2.5-flash)
- GPT-4o-mini — [devtk.ai](https://devtk.ai/en/models/gpt-4o-mini/)、[OpenAI 官方发布](https://openai.com/index/gpt-4o-mini-advancing-cost-efficient-intelligence/)、[pricepertoken.com](https://pricepertoken.com/pricing-page/model/openai-gpt-4o-mini)

注意：Gemini 2.5 Flash 如启用 thinking 模式，输出 token 量会显著放大（thinking token 也算价），实际账单可能更高。

---

## Q5. 已知系统性失败模式

### GPT-4o-mini
- **英文 bias**：在大批 r/LocalLLaMA 讨论中，开发者反复提到 GPT-4o-mini 在英文指令 + 中文内容场景下可靠，但纯中文 system prompt 下偶尔降级为简体/繁体混用。**无定量数据**，仅是开发者 anecdotal 反映。
- Structured output 在 **数组嵌套 + 可选字段 + union type** 场景下，官方 strict mode 之外的模式会忽略 schema。
- Function call 并行（parallel tool calls）在 mini 档位稳定性弱于 full 4o。

### Gemini 2.5 Flash
- **Reddit r/LocalLLaMA 帖"Need a replacement for Gemini 2.5 Flash Lite that's competent across all common languages"**（2026-03-19）显示开发者对 Flash-Lite 的多语言能力不满意。对完整版 Gemini 2.5 Flash，**无类似抱怨**。来源：[Reddit](https://www.reddit.com/r/LocalLLaMA/comments/1rya1yr/need_a_replacement_for_gemini_25_flash_lite_thats/)
- Thinking 模式 + streaming + tools 三元组合会触发 MALFORMED_FUNCTION_CALL（见 Q3）——如果你的 Observer 同时用这三项，**需要关闭 streaming 或 thinking**。
- Gemini 的安全过滤在处理**日常中文家庭对话**时，偶尔误判（如"教训小孩"、"打针"等词），被多位开发者提及（Reddit/HN 多贴，但**没有权威定量来源**——标记为 anecdotal）。
- Gemini 2.5 Flash 模型卡（[Google DeepMind](https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-2-5-Flash-Model-Card.pdf)）未披露中文特定失败模式。

---

## 针对 Jarvis（中文家庭语音场景）的倾向性建议

**倾向：保持 GPT-4o-mini，除非有具体痛点驱动切换。Confidence: Low-Medium。**

理由（按证据强度排序）：

1. **成本确定性**：Jarvis 的 Observer 跑在 cold path，batch 频率和 token 量可能不小。GPT-4o-mini 输出便宜 4×，长期成本优势明显——这是**唯一硬数据**。
2. **生态成熟度**：GPT-4o-mini 的 strict structured outputs 模式（2024-08 后）在 JSON schema 强约束场景比 Gemini 的 JSON mode 更成熟，社区 workaround 更多。
3. **你已有验证**：你的 MEMORY.md 记录 Observer 现在用 GPT-4o-mini 且已在生产跑过。切换 model 是**重验证成本**，不是零成本。
4. **Mastra 用 Gemini 2.5 Flash 是英文基准上的选择**（LongMemEval 是英文 longmemeval_s 数据集）。不能外推到中文。

**什么情况下应该切换**：
- 你跑小样本（20~50 条中文家庭对话）测出 GPT-4o-mini 的 observation 抽取**具体失败模式**（漏实体、JSON 损坏、语义错误），再对比 Gemini 2.5 Flash 看是否解决。
- 你需要 **>128K 上下文**（Gemini 有 1M）——但 Observer 单次输入通常不超过一次会话，不是瓶颈。
- 你已经在用 Mastra 全家桶且想减少 provider（非当前情况）。

**反对直接默认切 Gemini 的理由**：
- 没有公开中文 extraction benchmark 证明 Gemini 2.5 Flash 胜出。
- 价格更贵。
- 有活跃的 function call / JSON mode bug 未解决（python-genai #2081）。

---

## Sources

1. [Mastra Observational Memory 研究](https://mastra.ai/research/observational-memory) — 默认 Observer = gemini-2.5-flash，英文 benchmark
2. [SuperCLUE 2025 年度报告 EN](https://www.cluebenchmarks.com/superclue_2025_en) — 中文综合榜单
3. [SuperCLUE 2025 报告（微信）](https://mp.weixin.qq.com/s/w9Jyt5e3BR-lvaIMEjTAew) — Gemini-3-Pro 第二
4. [Google Gemini 2.5 Tech Report](https://arxiv.org/pdf/2507.06261) — 官方模型报告
5. [Gemini 2.5 Flash Model Card](https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-2-5-Flash-Model-Card.pdf) — 官方卡片
6. [OpenAI GPT-4o-mini 发布](https://openai.com/index/gpt-4o-mini-advancing-cost-efficient-intelligence/) — 官方声明
7. [docsbot Gemini 2.5 Flash vs GPT-4o-mini](https://docsbot.ai/models/compare/gemini-2-5-flash/gpt-4o-mini) — 只有英文 benchmark
8. [llm-stats 对比](https://llm-stats.com/models/compare/gemini-2.5-flash-vs-gpt-4o-mini-2024-07-18) — 无中文数据
9. [Gemini 2.5 Flash pricing — devtk.ai](https://devtk.ai/en/models/gemini-2-5-flash/)
10. [GPT-4o-mini pricing — devtk.ai](https://devtk.ai/en/models/gpt-4o-mini/)
11. [python-genai MALFORMED_FUNCTION_CALL #2081](https://github.com/googleapis/python-genai/issues/2081) — Gemini 2.5 Flash bug
12. [agno Gemini 2.5 JSON mode + fn call 冲突 #6655](https://github.com/agno-agi/agno/issues/6655)
13. [genkit Gemini 2.5 Flash Lite Python syntax tool call #3772](https://github.com/genkit-ai/genkit/issues/3772)
14. [OpenAI Community: Structured Outputs not reliable](https://community.openai.com/t/structured-outputs-not-reliable-with-gpt-4o-mini-and-gpt-4o/918735)
15. [OpenAI Community: Function JSON Schema ignored](https://community.openai.com/t/function-json-schema-is-still-ignored-by-gpt-4-4o-and-4o-mini-when-calling-tools/895368)
16. [OpenAI Community: complex function call parsing](https://community.openai.com/t/gpt-4o-mini-cant-parsing-complicate-function-callings-result-but-user-message-parsing-is-good/916805)
17. [Reddit: Gemini 2.5 Flash Lite 多语言不足](https://www.reddit.com/r/LocalLLaMA/comments/1rya1yr/need_a_replacement_for_gemini_25_flash_lite_thats/)
18. [DynamicNER EMNLP 2025](https://arxiv.org/html/2409.11022v4) — 多语言 NER benchmark，无 Flash/mini 档
19. [AI-Compass 中文 LLM 评测框架](https://www.nowcoder.com/discuss/776560061471563776)

---

## Methodology

- 9 次 exa 搜索 + 5 次 crawling_exa 深度抓取，覆盖 19 个独立来源
- 覆盖：官方 model card、arxiv 论文、OpenAI/Google 官方社区、GitHub issues、SuperCLUE 中文榜、Reddit、价格聚合站
- 尝试过的查询关键词：中文 NER benchmark、SuperCLUE、Chinese information extraction、function calling reliability、pricing、LocalLLaMA developer experience
- **故意未引用训练数据**：所有数字都附 URL

## Gaps / 诚实声明

- **核心缺口**：两款模型在中文 observation extraction / NER / structured output 上**没有公开 head-to-head benchmark**。此调研的决策支持度为 Medium-Low。
- 若决策权重高，建议花 1~2 小时自建 30 条中文家庭对话测试集，在两个模型上跑 precision/recall/JSON valid rate。这比任何第三方 benchmark 都更可靠。
