# LLM Tool-Use Schema 最佳实践 — 调研报告

*Generated: 2026-04-15 | Sources: 12 | Confidence: High*

---

## Executive Summary

tool definition 是 LLM function calling 中**投入产出比最高的优化点**。OpenAI 内部数据显示无 schema 约束时 JSON 合规率 <40%，启用 strict mode 后达到 100%。参数描述加具体示例可将准确率从 72% 提升到 90%（Anthropic 内部测试）。tool 数量在 25-50 个之间开始明显劣化，OpenAI 硬限制 128 个，推荐 <20 个。Grok 与 OpenAI schema 格式完全兼容但不支持 strict mode。

---

## Q1. OpenAI Strict Mode — 完整字段清单 + 限制

### Function Definition Schema

```jsonc
{
  "type": "function",           // 固定值
  "name": "get_weather",        // snake_case, ≤64 chars（推荐）
  "description": "...",         // 何时/如何用，输出含义
  "parameters": {               // 标准 JSON Schema
    "type": "object",
    "properties": { ... },
    "required": ["..."],        // strict 模式下必须列出全部字段
    "additionalProperties": false  // strict 模式下必须为 false
  },
  "strict": true                // 启用 constrained decoding
}
```

### Strict Mode 内部机制

- 将 JSON Schema 编译为 **context-free grammar (CFG)**
- 在 **logit 级别**约束 token 生成（不是 prompt 指令，是硬约束）
- Grammar 按 developer/org 缓存，首次请求有额外延迟
- Responses API **默认开启** strict（会自动补 `additionalProperties: false` 和 `required`）
- Chat Completions API **默认关闭** strict

### Strict Mode 限制

| 限制 | 说明 |
|------|------|
| `additionalProperties: false` | 每个 object 层级都必须设 |
| `required` 必须列全 | 所有 properties 都必须在 required 中 |
| 可选字段用 `null` | `"type": ["string", "null"]` |
| 部分 JSON Schema 不支持 | 不支持 `format` 约束（如 `"format": "email"` 会被忽略）、`pattern`、`if/then/else`、`$ref`（跨定义）等 |
| Fine-tuned + parallel calls | 同时多个 tool call 时 strict 被禁用 |
| Schema 不参与 ZDR | Fine-tuned 模型的 cached schema 不纳入 zero data retention |

### 新特性（2025-2026）

| 特性 | 说明 |
|------|------|
| **Namespaces** | `"type": "namespace"` 按领域分组工具（如 crm, billing） |
| **Tool Search** | GPT-5.4+ 支持，延迟加载不常用工具 |
| **Custom Tools** | 自由文本输入（非 JSON），可配 CFG 语法约束 |
| **Responses API** | 替代 Chat Completions 的 agent-native API，内部处理 tool loop |
| `allowed_tools` | tool_choice 新选项，限制可调用子集（配合 prompt caching） |

---

## Q2. Description 写法 — Dos & Don'ts

### ✅ DO

| 实践 | 说明 | 来源 |
|------|------|------|
| **Intent-based 命名** | `search_customer_orders` > `search` > `query` | tianpan.co |
| **描述 = 目的 + 格式 + 示例** | `"Order date in ISO 8601. Example: '2025-10-12'. Do not include time."` | Anthropic internal |
| **加具体示例** | 准确率 72% → 90%（Anthropic 内部测试）| tianpan.co |
| **enum 代替 free string** | `["open", "in_progress", "resolved"]` 而非开放字符串 | OpenAI + 多源 |
| **语义化参数名** | `user_email` > `user_id`（模型能推理 email 但无法推理 UUID） | tianpan.co |
| **说明输出含义** | 不只说参数是什么，还说返回什么 | OpenAI 官方 |
| **说明何时不用** | 在 system prompt 中说明何时该/不该用某工具 | OpenAI 官方 |
| **Intern Test** | 一个实习生只看你给模型的信息，能正确调用吗？ | OpenAI 官方 |

### ❌ DON'T

| 反模式 | 原因 |
|--------|------|
| **模糊命名**（`process`, `handle`, `do_thing`） | 路由信号弱，模型选错 tool |
| **省略参数描述** | 模型猜测→猜错→填错参数 |
| **开放字符串当分类用** | 模型会发明不存在的值 |
| **让模型填你已知的值** | 如 `order_id` 你已经有了，就别暴露这个参数 |
| **两个总是连续调用的函数分开** | 合并成一个，减少 round-trip |
| **description 过长** | 消耗 context tokens，但比没有好 |
| **reasoning 模型中加过多 examples** | OpenAI 官方警告：可能影响 reasoning 模型表现 |

### Description 模板

```
最佳格式：
"{做什么}。{何时用/不用}。参数 X 是 {含义}，格式 {format}，例如 '{example}'。"

示例：
"Retrieve current weather for a city. Use when user asks about weather conditions.
 location: City and country, e.g. 'Bogotá, Colombia'.
 units: Temperature unit, one of celsius/fahrenheit."
```

---

## Q3. Tool 数量 vs 准确率

### SixDegree Boundary 基准测试（2026-03）

150 个真实 MCP schema，6 个模型，5 个 toolset 大小，60 个 prompt/size。

| 模型 | 25 tools | 50 tools | 75 tools | 100 tools | 150 tools |
|------|----------|----------|----------|-----------|-----------|
| **Grok 4.1 Fast** | **86.7%** | 83.3% | 80.0% | 83.3% | **76.7%** |
| GPT-5.4 Mini | 85.0% | 85.0% | 80.0% | 83.3% | ❌ FAIL |
| GPT-4o | 81.7% | 78.3% | 73.3% | 76.7% | ❌ FAIL |
| Claude Haiku 4.5 | 81.7% | 80.0% | 78.3% | 80.0% | 76.7% |
| Grok 4 | 80.0% | 78.3% | 80.0% | 71.7% | 80.0% |
| Claude Sonnet 4.6 | 78.3% | 73.3% | 73.3% | 76.7% | 75.0% |

### 关键发现

| 维度 | 结论 |
|------|------|
| **劣化起点** | **25-50 tools 之间开始明显** |
| **OpenAI 硬限制** | **128 tools/request**（超过直接 API 报错） |
| **Grok 硬限制** | 200 tools/request |
| **Anthropic** | 无文档记录的硬限制 |
| **OpenAI 推荐** | **< 20 tools at start of turn** |
| **最佳方案** | Progressive disclosure — 每轮只暴露 10-20 个相关工具 |

### 模糊 prompt 更惨

当用户不指明服务名时（如 "check monitoring alerts" 而非 "check Datadog alerts"）：

| 模型 | 25t 模糊 | 100t 模糊 |
|------|----------|-----------|
| GPT-5.4 Mini | 92% | 92% |
| Claude Sonnet 4.6 | 83% | 83% |
| Grok 4 | 67% | **50%**（硬币翻转） |
| GPT-4o | 83% | **58%** |

### 常见混淆对

- Datadog ↔ Grafana（同为监控）
- Notion ↔ Confluence（同为文档）
- Linear ↔ Jira（同为项目管理）
- GitHub ↔ GitLab（同为代码托管）
- `terraform_create_run` ↔ `terraform_list_workspaces`（语义接近的同服务工具）

### 延迟影响

| 模型 | 25 tools | 150 tools | 增幅 |
|------|----------|-----------|------|
| GPT-5.4 Mini | 739ms | — | — |
| Claude Sonnet 4.6 | 4.7s | **28s** | **6x** |
| Grok 4.1 Fast | 6.4s | 7.5s | **1.2x** |

### Token 成本

- 3 个 MCP 服务（GitHub+Slack+Sentry）= **143K tokens**（200K 窗口的 72%）
- Tool definitions 是 **静态成本**：每次请求都付，不管用不用
- 1000 req/day + 大量 tool schema → $5000+/month 纯 schema overhead

### 缓解策略

1. **Domain-grouped loading** — 先识别意图，再加载对应 tool 组
2. **Tool search**（OpenAI GPT-5.4+）— 模型按需搜索工具，准确率 49% → 74%
3. **Result summarization** — 大结果摘要后再注入 context

---

## Q4. Grok vs OpenAI Schema 兼容性

### 完全兼容的部分

```jsonc
// 这份 schema 在 OpenAI 和 Grok 上都能直接用
{
  "type": "function",
  "name": "get_weather",
  "description": "...",
  "parameters": {
    "type": "object",
    "properties": {
      "location": { "type": "string", "description": "..." },
      "unit": { "type": "string", "enum": ["celsius", "fahrenheit"] }
    },
    "required": ["location"]
  }
}
```

| 特性 | OpenAI | Grok | Anthropic |
|------|--------|------|-----------|
| **Schema 格式** | JSON Schema | 同 OpenAI | JSON Schema（`input_schema` 键名） |
| **Strict mode** | ✅ `strict: true` | ❌ 不支持 | ✅ `strict: true` |
| **Tool 上限** | 128/request | 200/request | 无文档限制 |
| **Parallel calls** | ✅ 默认开 | ✅ 默认开 | ✅ |
| **Tool choice** | auto/required/none/forced/allowed_tools | auto/required/none/forced | auto/any/none/forced |
| **Streaming** | 分 chunk 流式 | **整个 function call 一个 chunk 返回** | 分 chunk 流式 |
| **Namespaces** | ✅（新）| ❌ | ❌ |
| **Tool search** | ✅ GPT-5.4+ | ❌ | ❌ |
| **Custom tools** | ✅ + CFG 语法 | ❌ | ❌ |
| **Schema 键名** | `parameters` | `parameters` | `input_schema` |
| **消息格式** | `role: "tool"` + `tool_call_id` | 同 OpenAI | `tool_result` content block |
| **系统 prompt 开销** | 未公开 | 未公开 | ~346 tokens（Claude 4.x） |
| **Cache control** | 通过 prompt caching | 未文档化 | `cache_control` 字段 |
| **Pydantic 集成** | ✅ SDK 原生 | ✅ `.model_json_schema()` | ✅ SDK 支持 |

### Grok 特有注意事项

1. **无 strict mode** — 依赖 prompt 指令 + 服务端验证
2. **Streaming 行为不同** — function call 不是流式，一次性返回完整结果
3. **200 tool 上限** — 比 OpenAI (128) 高，但根据 benchmark 数据 >25 就开始劣化
4. **无已知私有扩展** — 纯粹兼容 OpenAI 格式

---

## 对 Jarvis 的实操建议

### 统一 YAML → Tool Definition 转换

```yaml
# skills/xxx.yaml 定义（L1 解释器用）
name: set_light
description: "Control smart light on/off/brightness. Use when user mentions lights or room lighting."
parameters:
  room:
    type: string
    enum: [living_room, bedroom, kitchen, bathroom]
    description: "Target room. Example: 'living_room'"
  action:
    type: string
    enum: [on, off, brightness]
    description: "Light action to perform"
  brightness:
    type: integer
    description: "Brightness level 1-100. Only needed when action is 'brightness'. Example: 75"
    required: false
```

转换时注意：
1. **OpenAI/Grok**: `parameters` 键 → 加 `additionalProperties: false` + 全部 `required`
2. **Anthropic**: `input_schema` 键 → 可选字段用 `"type": ["integer", "null"]`
3. **Optional 字段**: strict mode 下用 `null` type 表达
4. **enum 一定要用** — 别让模型自由填字符串

### Tool 数量控制

Jarvis 当前 skill 数量在安全范围内（<20），如果未来扩展：
- **Phase 1** (≤20 tools): 全部直传，无需优化
- **Phase 2** (20-50 tools): 按 intent 分组加载（设备控制组 / 信息查询组 / 系统管理组）
- **Phase 3** (50+ tools): 两步路由 — 先 intent → 再加载对应 tool 子集

### Description 写法模板

```
"{动作}。{使用场景}。"
参数: "{含义}，格式 {format}。例如 '{example value}'。{约束说明}"
```

---

## Sources

1. [OpenAI Function Calling Guide](https://platform.openai.com/docs/guides/function-calling) — 官方完整指南，含 strict mode、namespaces、tool search
2. [OpenAI Structured Outputs Guide](https://platform.openai.com/docs/guides/structured-outputs) — strict mode 内部机制 + 支持的 schema 子集
3. [Anthropic Tool Use Overview](https://docs.anthropic.com/en/docs/build-with-claude/tool-use) — Claude tool definition schema + 定价 + cache_control
4. [xAI Grok Function Calling](https://docs.x.ai/docs/guides/function-calling) — Grok schema 兼容性 + 200 tool 限制 + streaming 差异
5. [SixDegree: We Gave LLMs 150 Tools](https://sixdegree.ai/blog/mcp-tool-overload) — ★★★ 6 模型 × 5 尺寸基准测试，含准确率/延迟/成本
6. [tianpan.co: Tool Use in Production](https://tianpan.co/blog/2025-10-12-tool-use-function-calling-patterns) — 实战经验：schema 设计 + enum + 示例描述 + 安全
7. [Groundy: Function Calling Best Practices](https://groundy.com/articles/function-calling-best-practices-llms-that-actually-use-apis/) — 综合指南 + API 演进 2025-2026 + MCP
8. [Hugo Nogueira: 100th Tool Call Problem](https://www.hugo.im/posts/100th-tool-call-problem) — 长期运行 agent 的退化模式 + checkpoint 策略
9. [Kusireddy: 340 Tools in Production](https://pub.towardsai.net/openai-function-calling-works-great-until-you-have-340-tools-12-tenants-real-production-traffic-fe02da116e39) — Fortune 500 生产环境 340 tools 实战
10. [youngju.dev: LLM Function Calling Guide](https://www.youngju.dev/blog/llm/2026-03-03-llm-function-calling-tool-use-guide.en) — OpenAI vs Anthropic 对比 + benchmark 数据
11. [OpenAI Community: Strict Mode Discussion](https://community.openai.com/t/strict-true-and-required-fields/1131075) — strict mode required 字段限制的实际影响
12. [OpenAI Community: Schema Enforcement](https://community.openai.com/t/strict-mode-does-not-enforce-the-json-schema/1104630) — strict mode 边界情况 + anyOf 使用

## Methodology

搜索 4 个官方文档 + exa 搜索 23 条结果 + 深度阅读 8 篇文章。交叉验证关键数据点（SixDegree benchmark 数据为 2026-03 最新）。
