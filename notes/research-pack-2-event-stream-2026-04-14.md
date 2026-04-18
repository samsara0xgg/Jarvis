# RESEARCH PACK 2 — Event-stream vs Turn-based 数据模型

日期：2026-04-14
问题：Jarvis（语音 + 传感器 + 主动提醒）该用 turn-based 还是 event-stream？

---

## 核心结论

2025–2026 年产线级 LLM observability 与 ambient agent 架构 **一致采用 append-only event stream**：会话（conversation / session）被降格为 attribute，不再是 storage primitive。Turn 结构只作为 LLM span 的 payload 存在，不再作为存储主键。

---

## 对比表

| 维度 | Turn-based (message array) | Event-stream (append-only log) |
|---|---|---|
| 主键 | `conversation → messages[]` | `events[]`，`conversation_id` 仅为外键 |
| 非对话事件 | 需特殊通道（sensor/timer 走旁路） | 一等公民，同表同 schema |
| 并行工具调用 | 破坏线性 turn 结构 | 同 `parent_id` 的 sibling spans |
| 主动提醒 / 打断 | 难塞进 turn，因果丢失 | `parent_id=NULL` 的独立事件 |
| 审计 / 回放 | 状态覆盖，丢中间信念 | 完整 replay + time-travel |
| schema 升级 | 改表迁移 | 只增 `type`，表不动 |
| 实现成本 | 低（直接序列化 history） | 需 envelope + projection 层 |
| 语音 + IoT 适配 | 笨拙 | 天然贴合 |
| 适用场景 | 短链纯 chat、demo | 长时运行、多信号、ambient assistant |

产线共识来源：OpenTelemetry GenAI semconv、Langfuse v4 observation-centric 迁移、Arize OpenInference、LangSmith runs、Helicone Sessions —— **无一例外** 都以 typed span/event 为原子，`conversation_id` 做关联属性。

---

## Schema 示例

### ① Generative Agents (Park 2023) — 单 node 多 type

```python
class ConceptNode:
  node_id, type       # "event" | "thought" | "chat"
  depth               # 0=原始观察，≥1=反思
  created, expiration, last_accessed
  subject, predicate, object   # s-p-o 结构化摘要
  description, embedding_key
  poignancy           # LLM 打分 1-10 重要度
  keywords            # 倒排索引 key
  filling             # 来源节点 ids，溯源链
```

观察、对话、反思 **共享同一 schema**，靠 `type` + `depth` 区分。反思就是 `depth>0` 且 `filling` 指向证据节点的 ConceptNode。这是最贴 Jarvis 的范式。

### ② Home Assistant Event Bus — 统一 envelope

```json
{
  "event_type": "state_changed",
  "time_fired": "2016-11-26T01:37:24.265429+00:00",
  "origin": "LOCAL",
  "context": {
    "id": "326ef27d…",
    "parent_id": null,
    "user_id": "31ddb…"
  },
  "data": {
    "entity_id": "light.bed_light",
    "old_state": {...},
    "new_state": {...}
  }
}
```

**关键是 `context{id, parent_id, user_id}`**：TTS 回复 → Hue 状态变化 → 原始语音 turn 事后可串成完整因果链，调试 streaming tool-use 循环必备。

### ③ OpenTelemetry GenAI — 工具调用 span

```yaml
span.name: "execute_tool get_weather"
span.kind: INTERNAL
attributes:
  gen_ai.operation.name: "execute_tool"
  gen_ai.tool.name: "get_weather"
  gen_ai.tool.call.id: "call_mszuSIzqtI65i1wAUOE8w5H4"
  gen_ai.conversation.id: "conv_5j66UpCpwteGg4YSxUnt7lPY"
```

同 `gen_ai.conversation.id` 的所有 span（chat / execute_tool / embeddings / invoke_agent）互相 sibling。这套 **10 种 span kind**（LLM / TOOL / RETRIEVER / EMBEDDING / CHAIN / AGENT / GUARDRAIL / EVALUATOR / PROMPT / EVENT）在 OpenInference、Langfuse、LangSmith 间几乎 1:1 一致，是现成的通用词汇表。

---

## 对 Jarvis 的推荐形态

**单表 append-only，Park × HA 混血**：

```sql
events(
  id              UUID PK,
  parent_id       UUID,           -- 因果树
  conversation_id UUID,           -- 本次对话，可为 NULL（后台 sensor）
  type            TEXT,           -- 见下节分类
  source          TEXT,           -- "mic" | "hue" | "timer" | "llm" ...
  created_at      TIMESTAMP,
  duration_ms     INT NULL,       -- 点事件 NULL，span 才有
  subject, predicate, object TEXT,-- s-p-o 摘要（反思用）
  description     TEXT,           -- 人读 / embed 文本
  embedding_key   TEXT,
  poignancy       SMALLINT,       -- LLM 打分，GC 用
  payload         JSONB,          -- 原始数据
  context_user_id UUID            -- 说话人 / 触发人
)
```

**好处**：

1. 语音 turn、传感器读数、定时提醒、反思 **全同构**；主动提醒天然是 `parent_id=NULL` 的事件。
2. 现有 turn-based 代码只需 `SELECT * WHERE conversation_id=? AND type IN (voice.turn, llm.generation, tool.invoked) ORDER BY created_at` 即可 reconstruct 成 message list 喂 LLM —— **向后兼容零迁移成本**。
3. 与 OTel 对齐，未来要接 Langfuse / Arize 只需 export 适配器。

---

## 推荐 event type 分类

按语义层分四组（对齐 OpenInference + HA）：

```
perception（输入）
  voice.wake                  唤醒词触发
  voice.turn                  完整语音（含 ASR / 声纹 / 情绪）
  sensor.state_changed        Hue / MQTT / 任何设备状态翻转
  time.tick                   定时器 / cron

cognition（决策）
  intent.routed               Groq 路由结果
  memory.recall               SQLite / FastEmbed 命中
  llm.generation              Grok / Groq 调用（span，可含 token 子事件）
  reflection.created          后台反思沉淀（depth≥1）

action（动作）
  tool.invoked                skills/* 的 execute
  device.command              permission_manager 下发的硬件指令
  tts.utterance               合成语音（含 engine / emotion）

meta（元）
  proactive.reminder          主动提醒触发（parent_id=NULL）
  session.interrupted         全双工打断
  error.raised                异常 / 降级
```

每类对齐一个 OpenInference span kind，保持未来 OTel 导出可能。

---

## 主要来源

1. OpenTelemetry GenAI semconv — opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
2. Langfuse data model (v4) — langfuse.com/docs/observability/data-model
3. Langfuse enhanced observation types (2025-08) — langfuse.com/changelog/2025-08-27-enhanced-observation-types
4. Arize OpenInference spec — github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md
5. Helicone Sessions — docs.helicone.ai/features/sessions
6. Generative Agents `associative_memory.py` — github.com/joonspk-research/generative_agents
7. Home Assistant WebSocket API — developers.home-assistant.io/docs/api/websocket
8. Amazon Alexa+ / Omnisense (2025-09) — aboutamazon.com
9. Tian Pan, "Agent state as event stream" (2026-04) — tianpan.co/blog
10. ESAA: Event Sourcing for Autonomous Agents — arXiv 2602.23193
11. Temporal Durable AI Agents — temporal.io/pages/durable-ai-agent-bundle
