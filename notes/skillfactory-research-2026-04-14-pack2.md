# Research Pack 2 — Event-stream vs Turn-based 数据模型调研

*Date: 2026-04-14 · Scope: 为 Jarvis 下一版架构选数据模型（主动提醒 + 传感器融合方向）*

## TL;DR

1. **行业共识：event-stream + nested spans 正在吞并 turn-based**。OTel GenAI semconv (stable 2025) 把整条 agent 对话建成 span 树，`gen_ai.input.messages` / `gen_ai.output.messages` 只是一个 span 上的属性，与 `invoke_agent` / `execute_tool` / `embeddings` / `chat` 平级为 8 个 operation name。[OTel gen-ai registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
2. **Langfuse v4 数据模型的核心升级就是去掉了"trace 本身有 input/output"的 turn 感，改成 observation-centric 的不可变事件表**（10 种 observation type：span/generation/agent/tool/chain/retriever/evaluator/embedding/guardrail/event）。[Langfuse 2025-08 changelog](https://langfuse.com/changelog/2025-08-27-enhanced-observation-types)
3. **Generative Agents 2023 的 memory stream 就是 event-stream 的原型**：每个 ConceptNode 都带 `type ∈ {event, thought, chat}` + SPO 三元组 + poignancy + embedding_key，统一表示观察 / 反思 / 对话。[associative_memory.py](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/memory_structures/associative_memory.py)
4. **Home Assistant 2013 年就是纯 event bus 架构**：`state_changed`、`call_service`、`EVENT_HOMEASSISTANT_START` 全走同一个 `EventBus.async_fire`，事件 payload 标准化为 `{entity_id, old_state, new_state, origin, time_fired, context}`。这正是 Jarvis 将来要融合传感器的目标形态。[HA event-handling](https://www.mintlify.com/home-assistant/core/guides/event-handling)
5. **Proactive agent 研究（NeurIPS 2025 ContextAgent、ProAgent）已经把 sensory context + persona 统一建模成 LLM 输入事件**，而不是额外搞一条旁路通道。[ContextAgent paper](https://openreview.net/pdf/8c61939b607693d9b13cc1df27793d844f3648f5.pdf)
6. **给 Jarvis 的直接结论：上 event-stream，但只保留一层"因果链"，不要搞 span 树**——因为 Jarvis 不是分布式多 agent，省掉 parent_span_id 的复杂度就能吃到 event-stream 90% 的好处。

## 1. Turn-based vs Event-stream 对比

| 维度 | Turn-based (USER/ASSISTANT msgs) | Event-stream (统一 envelope) |
|---|---|---|
| 表达能力 | 只能表达轮次；传感器事件要硬塞到 system message 或 user message 里 | 原生支持非轮次：`device_state_change`、`timer_fired`、`proactive_trigger` |
| 复杂度 | 低：一个 list[{role, content}] 就完事 | 中：需要 event type 枚举 + payload schema + envelope 字段 |
| 工具生态 | OpenAI / Anthropic API 原生格式；LLM context 注入零成本 | 注入 LLM 时需要 projection/序列化；但可一键兼容 OTel/Langfuse |
| 主动提醒 | 必须"伪造"一条 user turn，语义扭曲 | 原生：append 一个 `proactive_trigger` event，LLM 按 persona 决定是否开口 |
| trace/debug | 只能在 role 级别看；tool_use 常要压进 assistant 里 | 每个 event 独立时间戳 + sequence_number，支持 time-travel replay ([UnderstandingData](https://understandingdata.com/posts/event-sourcing-agents/)) |
| LLM context 注入 | 天然 role 格式 | 需要一个 `to_messages(events)` projection 函数（~20 行） |
| 存储/查询 | 简单 JSON list；按 session_id 取 | 单表 `events` + (thread_id, sequence_number) 索引；支持 `WHERE type='tool_invocation'` 统计 |
| schema 演化 | 改 role 语义要迁移全部历史 | append-only + `schema_version` 字段，旧事件按 upcast 处理 |
| 中断/续跑 | 断点状态难表达 | 天然：重放事件到 last successful 即可恢复 ([Event Sourcing for Agents](https://understandingdata.com/posts/event-sourcing-agents/)) |
| 多用户 / 声纹 | 要把 `speaker_id` 塞 metadata | 原生 envelope 字段 |
| 异步回调 | 没地方放 | `tool_invocation` 发出后 `tool_result` 异步 append，带 `parent_event_id` |

## 2. 生产 schema 示例（抄录原字段）

### 2.1 OpenTelemetry GenAI semantic conventions (stable Oct 2025)

`gen_ai.operation.name` 的 8 个合法值：

```
chat              — Chat completion (OpenAI Chat API 类)
create_agent      — Create GenAI agent
embeddings        — Embeddings 操作
execute_tool      — 执行工具
generate_content  — 多模态生成（Gemini 类）
invoke_agent      — 调用 GenAI agent（2025-04 PR #2160 合入）
retrieval         — 检索操作（OpenAI Search Vector Store 类）
text_completion   — Legacy text completion
```

核心 attributes（节选，全部 30+ 条）：

```
gen_ai.conversation.id           # session/thread id，跨 span 关联
gen_ai.agent.id / .name / .version / .description
gen_ai.operation.name            # 上面 8 选 1
gen_ai.provider.name             # openai / anthropic / x_ai / groq / ...
gen_ai.request.model             # gpt-4, grok-4.1-fast, ...
gen_ai.input.messages            # JSON 数组，含 role + parts (text/tool_call/tool_call_response)
gen_ai.output.messages           # 同上，返回值
gen_ai.tool.call.id / .arguments / .result
gen_ai.tool.definitions          # 可用工具列表（大，默认不开）
gen_ai.retrieval.documents       # [{id, score}, ...]
gen_ai.retrieval.query.text
gen_ai.usage.input_tokens / .output_tokens / .cache_read.input_tokens
gen_ai.evaluation.name / .score.value / .score.label / .explanation
```

`gen_ai.input.messages` 示例（工具调用流程完整表达为一个 JSON 数组）：

```json
[
  {"role": "user",
   "parts": [{"type": "text", "content": "Weather in Paris?"}]},
  {"role": "assistant",
   "parts": [{"type": "tool_call", "id": "call_VSPygqK...",
              "name": "get_weather",
              "arguments": {"location": "Paris"}}]},
  {"role": "tool",
   "parts": [{"type": "tool_call_response", "id": "call_VSPygqK...",
              "result": "rainy, 57°F"}]}
]
```

来源：[opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)。**关键观察**：OTel 是**嵌套 span 树**，但 span 属性 `gen_ai.input.messages` 内部仍是**turn-based 的 list**——属于"外层 event-stream、内层 role-based"的混合方案。`gen_ai.system` 已被 deprecate，改用 `gen_ai.provider.name`（`x_ai` 已列入标准）。

### 2.2 Langfuse v4 observation-centric 数据模型

10 种 observation type（2025-08 扩展）：

| Type | 类别 | 模型字段 | 用途 |
|---|---|---|---|
| `span` | 通用 | 无 | 默认，任意操作 |
| `generation` | generation-like | 有 model, usage, cost | LLM 补全 |
| `embedding` | generation-like | 有 model, usage, cost | 向量生成 |
| `agent` | span-like | 无 | autonomous 多步流程 |
| `tool` | span-like | 无 | 外部 API / 函数调用 |
| `chain` | span-like | 无 | 顺序 pipeline |
| `retriever` | span-like | 无 | 向量库 / RAG |
| `evaluator` | span-like | 无 | 评分 |
| `guardrail` | span-like | 无 | 安全 / 越狱检查 |
| `event` | special | 无 | 瞬时离散事件（start==end，不用 `.end()`） |

通用字段（所有类型共享）：

```
id (auto)
trace_id (auto)
name (str, required)
input / output / metadata (JSON-serializable)
version (str)
level: SpanLevel  # INFO / WARNING / ERROR / DEBUG
status_message (str)
```

Generation-like 追加字段：

```
model (str)                  # "gpt-4", "grok-4.1-fast"
model_parameters (dict)      # {"temperature": 0.7, "max_tokens": 500}
usage_details (dict)         # {"input": 100, "output": 50, "total": 150}
cost_details (dict)
completion_start_time (ts)
prompt (PromptClient)
```

来源：[Langfuse data-model](https://langfuse.com/docs/observability/data-model) + [DeepWiki observation-types](https://deepwiki.com/langfuse/langfuse-python/2.2-observation-types)。**关键观察**：Langfuse v4 的 **context 属性（user_id/session_id/metadata/tags）被下沉到每个 observation**，取消 trace 本身的 input/output，做到 "immutable observation = event"。这是标准 event sourcing 的做法。

### 2.3 OpenInference Span Kinds（Arize Phoenix）

`openinference.span.kind` 必须取值：

```
LLM / EMBEDDING / CHAIN / RETRIEVER / RERANKER /
TOOL / AGENT / GUARDRAIL / EVALUATOR / PROMPT
```

reserved attributes 节选：

```
document.content / .id / .metadata / .score
embedding.embeddings[] / .model_name / .vector / .text
llm.input_messages[] / .output_messages[] / .model_name / .invocation_parameters
tool.name / .description / .parameters
retrieval.documents[]
exception.message / .stacktrace / .type / .escaped
```

来源：[openinference/spec/semantic_conventions.md](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md)。**关键观察**：和 Langfuse 10 类几乎一一对应，说明业界正在**收敛到 10 个核心 kind**，其中没有一个叫 `user_turn` 或 `assistant_turn`——轮次概念被彻底下沉成 `LLM` span 的 input/output 属性。

### 2.4 Generative Agents (Park et al. 2023) Memory Stream

`ConceptNode` 完整字段（每个节点 = 一个 memory event）：

```python
class ConceptNode:
    node_id        # "node_1", "node_2", ...
    node_count     # 全局序号（跨 type）
    type_count     # 类型内序号
    type           # "event" | "thought" | "chat"  ← 三选一
    depth          # event=0, chat=0, thought=1+max(parents)
    created        # datetime
    expiration     # datetime or None
    last_accessed  # datetime
    subject, predicate, object   # SPO 三元组
    description    # 自然语言描述
    embedding_key  # 查 embeddings.json
    poignancy      # 1-10，重要性评分（GPT 打）
    keywords       # set[str]，用于 kw_to_event 倒排
    filling        # 指向其他 node_id（thought 的 "based on" 引用链）
```

持久化：`nodes.json` + `embeddings.json` + `kw_strength.json` 三个文件。

来源：[joonspk-research/generative_agents/.../associative_memory.py](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/memory_structures/associative_memory.py)。**关键观察**：
- 只有 3 个 type 但语义覆盖广：`event` = 观察到的客观发生，`thought` = agent 反思产出，`chat` = 对话（完整 turn 列表存在 `filling` 里）。
- `event` 和 `chat` 的区分点：chat 的 `object` 是另一个 persona，filling 是完整对白数组；event 的 object 是动作对象。
- `poignancy` 字段启发：**Jarvis 可以给每个事件打重要性分，用于 decay 策略和主动提醒触发**。
- depth 字段表达因果链（反思链可追溯到原始事件）——和现代 event sourcing 的 `parent_event_id` 同构。

### 2.5 Home Assistant EventBus 结构

事件类型（HA core 常量）：

```
EVENT_STATE_CHANGED        # 实体状态变化（最高频）
EVENT_STATE_REPORTED       # 实体状态刷新但未变
EVENT_HOMEASSISTANT_START / _STARTED / _STOP
EVENT_SERVICE_REGISTERED   # 服务注册
EVENT_CALL_SERVICE         # 服务被调用
```

`state_changed` 的 `event.data` 典型 payload：

```python
{
  "entity_id": "light.living_room",
  "old_state": State(state="off", attributes={...}, last_changed=..., last_updated=...),
  "new_state": State(state="on",  attributes={"brightness":200}, last_changed=..., last_updated=...),
}
# + event 外层：
#   event_type: "state_changed"
#   origin: EventOrigin.local / remote
#   time_fired: datetime
#   context: Context(id=..., parent_id=..., user_id=...)  ← 因果链
```

订阅 API：

```python
hass.bus.async_listen(EVENT_STATE_CHANGED, handle_event, event_filter=fn)
# 辅助函数：
async_track_state_change_event(hass, entity_ids, cb)      # 只盯特定实体
async_track_template_result(hass, [TrackTemplate(...)])    # 模板计算变更
async_track_point_in_utc_time(hass, cb, when)              # 定时触发
```

来源：[mintlify.com/home-assistant/core/guides/event-handling](https://www.mintlify.com/home-assistant/core/guides/event-handling) + [developers.home-assistant.io/docs/integration_events](https://developers.home-assistant.io/docs/integration_events/)。**关键观察**：HA 的 `Context(id, parent_id, user_id)` 就是跨 event 的因果追踪——Jarvis 语音触发的设备操作可以把语音 event 的 id 作为 parent_id，天然串起"用户说话 → Hue 开灯"。

### 2.6 Event-sourced agent envelope (2026 推荐实践)

来自独立研究者 James Phoenix 的生产建议（[UnderstandingData 2026-02](https://understandingdata.com/posts/event-sourcing-agents/)）：

```typescript
interface AgentEvent {
  id: string                 // 唯一 event id
  sequenceNumber: number     // 单调递增，**线程内排序靠这个，不是 timestamp**
  threadId: string           // 哪个 agent 线程
  timestamp: Date
  type: string               // 见分类
  category: "lifecycle" | "action" | "result" | "human" | "system"
  payload: Record<string, unknown>
  parentEventId?: string     // 因果链
  correlationId?: string     // 跨线程关联
  schemaVersion: number
}
```

事件分类实例：`thread_created` / `tool_called` / `tool_succeeded` / `tool_failed` / `approval_requested` / `approval_granted` / `human_feedback` / `context_compacted` / `checkpoint_created` / `rate_limited` / `budget_warning`。

命名规则：**过去时、具体语义、自包含 payload**（不要 `file_changed` 带 changeType，要 `file_created / file_modified / file_deleted` 三个独立 type）。

## 3. 对 Jarvis 的推荐

### 推荐形态：**轻量 event-stream（扁平 + 因果指针）**

不上完整 span 树。理由：

1. Jarvis 是**单体语音管家**，不是分布式多 agent，没有跨服务 span propagation 的必要。
2. 当前 `session.py` 就是 turn-based 的 list[Message]，一次性切换到 span 树成本过高，**中间方案是扁平 event list + 可选 `parent_event_id`**。
3. OTel / Langfuse 内部实现上 span 本质就是带 start/end 的 event，**Jarvis 可以全部用 `event` 瞬时事件表达，省掉 span 的生命周期管理**。

### 分阶段迁移路径

**Phase 1（不动存量）**：在现有 `SessionState` 旁加一个 `EventLog`，先只 append 不读，用作 trace。20% 工作量拿到 trace/debug + 可重放。

**Phase 2（双写双读）**：新的 proactive、sensor-trigger、farewell-shortcut 走 event；传统对话仍 turn。LLM 入参通过 `to_messages(events)` projection 合流。

**Phase 3（完全 event）**：turn list 降级成 projection，`SessionState` 变成 events 的投影视图。

## 4. 推荐的 event type 分类（Jarvis 专用）

借鉴 Generative Agents 的 3-type 极简 + Langfuse 的 10-type 明确化，取折中 **11 类**：

```python
# 公共 envelope（所有 event 都有）
{
  "event_id": "evt_0001a2b3",
  "seq": 147,                    # thread 内单调递增
  "thread_id": "sess_20260414_2131",
  "ts": "2026-04-14T21:31:05.123Z",
  "type": "...",                 # 下面 11 选 1
  "speaker_id": "allen",         # 声纹识别结果，可为 None
  "parent_event_id": "evt_000..."  # 因果链（可选）
  "schema_version": 1,
}
```

### 1. `user_utterance` — 用户说话（含声纹 + ASR）
```python
payload = {
  "text": "把客厅灯调暖一点",
  "asr_backend": "sensevoice-int8",
  "asr_confidence": 0.92,
  "asr_latency_ms": 180,
  "voiceprint_score": 0.88,
  "role": "owner",              # 4-tier 权限
  "audio_ref": "audio/2026-04-14/21_31_05.wav",  # 可选保留
  "vad_segments": [[120, 1800]],
}
```

### 2. `assistant_response` — Jarvis 回复（文本 + TTS meta）
```python
payload = {
  "text": "好的，亮度调到 40%，色温 2700K",
  "tts_engine": "minimax",       # minimax / edge-tts / pyttsx3
  "emotion": "neutral",
  "tts_latency_ms": 420,
  "streamed_chunks": 4,          # TTS 双线程切片数
  "interrupted": false,
}
```

### 3. `tool_invocation` — 本地技能 / skill 调用
```python
payload = {
  "tool_name": "hue_adjust",
  "arguments": {"entity": "living_room", "brightness": 40, "color_temp": 2700},
  "invocation_id": "call_abc",
  "invoked_by": "intent_router",  # intent_router / llm_tool_use / proactive
  "dry_run": false,
}
```

### 4. `tool_result` — 工具执行结果（异步回填）
```python
payload = {
  "invocation_id": "call_abc",   # 对应上面 tool_invocation
  "success": true,
  "result": {"prev_brightness": 80, "new_brightness": 40},
  "duration_ms": 230,
  "error": null,                 # 失败时填 {type, message, retryable}
}
# parent_event_id = tool_invocation 的 event_id
```

### 5. `llm_call` — Cloud LLM 一次请求（单独 event，便于成本/延迟分析）
```python
payload = {
  "provider": "x_ai",             # OTel 标准值
  "model": "grok-4.1-fast",
  "operation": "chat",            # OTel gen_ai.operation.name
  "input_messages_ref": "llm_ctx/evt_..._in.json",   # 大 payload 外存
  "output_text": "好的，亮度调到 40%",
  "input_tokens": 1240,
  "output_tokens": 32,
  "cache_read_tokens": 800,
  "ttfb_ms": 480,
  "total_ms": 920,
  "finish_reason": "stop",
}
```

### 6. `memory_recall` — 记忆检索（FastEmbed + SQLite）
```python
payload = {
  "query_text": "客厅灯偏好",
  "retrieved": [
    {"id": "mem_123", "score": 0.91, "text": "Allen 喜欢 2700K 暖光"},
    {"id": "mem_089", "score": 0.84, "text": "客厅默认亮度 40%"}
  ],
  "latency_ms": 45,
  "source": "sqlite+fastembed",
}
```

### 7. `device_state_change` — 设备状态变化（来自 Hue/MQTT/sim）
```python
payload = {
  "entity_id": "light.living_room",
  "old_state": {"on": true, "brightness": 80},
  "new_state": {"on": true, "brightness": 40, "color_temp": 2700},
  "source": "hue_live",           # hue_live / mqtt / sim
  "attributed_to": "evt_...",     # 归因：哪个 event 触发的
}
```

### 8. `sensor_event` — 未来传感器（门磁 / 运动 / 环境）
```python
payload = {
  "sensor_id": "front_door_contact",
  "sensor_type": "contact",       # contact / motion / lux / temp / sound
  "value": "open",
  "previous_value": "closed",
  "location": "entryway",
  "raw": {"battery": 87},
}
```

### 9. `timer_fired` — 定时提醒 / 日程
```python
payload = {
  "timer_id": "reminder_0412_0930",
  "scheduled_for": "2026-04-14T09:30:00Z",
  "kind": "reminder",             # reminder / routine / cron
  "payload_text": "该吃药了",
  "created_by_event": "evt_...",
}
```

### 10. `proactive_trigger` — 主动推送决策点（决定说/不说的那一刻）
```python
payload = {
  "trigger_source": "sensor_event:front_door_contact" ,   # 哪个事件触发推理
  "hypothesis": "allen 回家了",
  "confidence": 0.78,
  "decision": "speak",            # speak / suppress / defer
  "reason": "首次进门 + TTS 静默时段已过",
  "cooldown_remaining_s": 0,
}
# parent_event_id 指向触发源 event
```

### 11. `system_event` — 系统状态（唤醒词、错误、生命周期）
```python
payload = {
  "kind": "wake_word_detected",   # wake_word_detected / vad_start / vad_end /
                                  # barge_in / error / startup / shutdown /
                                  # ctx_compacted / rate_limited
  "detail": {"score": 0.94, "model": "hey_jarvis_v0.1"},
}
```

---

**LLM context projection**（把 event list 喂给 Grok-4.1-fast）：

```python
def to_messages(events, window_n=20):
    msgs = []
    for e in events[-window_n:]:
        if e.type == "user_utterance":
            msgs.append({"role": "user", "content": e.payload["text"]})
        elif e.type == "assistant_response":
            msgs.append({"role": "assistant", "content": e.payload["text"]})
        elif e.type == "tool_invocation":
            msgs.append({"role": "assistant",
                         "content": [{"type":"tool_use", "id": e.payload["invocation_id"],
                                      "name": e.payload["tool_name"],
                                      "input": e.payload["arguments"]}]})
        elif e.type == "tool_result":
            msgs.append({"role":"user",
                         "content":[{"type":"tool_result",
                                     "tool_use_id":e.payload["invocation_id"],
                                     "content": json.dumps(e.payload["result"])}]})
        elif e.type == "sensor_event":
            # 传感器以 system 注入，不占 user turn
            msgs.append({"role":"system",
                         "content":f"[sensor] {e.payload['sensor_id']}: "
                                   f"{e.payload['previous_value']} → {e.payload['value']}"})
        elif e.type == "proactive_trigger" and e.payload["decision"] == "speak":
            # 主动提醒作为 user turn 注入，让 LLM 生成自然措辞
            msgs.append({"role":"user",
                         "content":f"[proactive] {e.payload['hypothesis']}"})
    return msgs
```

## 5. 风险 & 未解决问题

1. **大 payload 膨胀**：LLM 原始 messages 动辄几十 KB，直接塞 event payload 会把 SQLite 胀成 GB 级。**缓解**：大字段外存（`input_messages_ref: "path/to/file"`），event 表只存指针。Langfuse v4 走的也是 media storage 方案。
2. **事件乱序**：异步 `tool_result`、传感器 event、proactive 推理可能跨线程。**缓解**：`sequence_number` 单调递增是单线程 append 时成立的；多 producer 场景需要 buffer 机制（参考 [UnderstandingData EventuallyConsistentStore](https://understandingdata.com/posts/event-sourcing-agents/)）——Jarvis 当前单进程，风险低。
3. **无法热插拔新 event type**：每次加 type 都要更新 projection。**缓解**：`schema_version` 字段 + unknown type 走 fallback（丢给 system role 注入）。
4. **主动提醒的"静默策略"放哪层**：是放在 `proactive_trigger` 的决策逻辑里，还是让 LLM 判断？ContextAgent 的做法是**双阶段**：小模型做 "need proactive?" 二分，LLM 只在 need=true 时生成措辞。Jarvis 可参考——保持低成本 + 可解释。[ContextAgent NeurIPS 2025](https://openreview.net/pdf/8c61939b607693d9b13cc1df27793d844f3648f5.pdf)
5. **OTel 兼容度**：如果未来要接 Langfuse/Phoenix 做 observability，event envelope 要能 1:1 映射到 OTel span。**缓解**：`type` 字段对齐 OTel 的 `gen_ai.operation.name` 和 OpenInference `span.kind`（我上面 11 类里 `llm_call`/`tool_invocation`/`memory_recall` 已对齐）。
6. **声纹字段 `speaker_id`** 不是每个 event 都有意义（`timer_fired` 就没有）。**缓解**：放 envelope 顶层但允许 None，别硬塞进每条 payload。
7. **"chat" vs "event"**：Generative Agents 里 chat 是一个 node，filling 存完整对白。Jarvis 更细粒度（每句一个 event），但要注意**检索/摘要时聚合单位**——建议在 query 层做 turn 聚合，而不是存储层。

## Sources

1. [OpenTelemetry GenAI Attributes Registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/) — stable 2025 semconv，8 个 operation name + 30+ gen_ai.* attributes，权威
2. [OTel semantic-conventions PR #2160: Add invoke_agent](https://github.com/open-telemetry/semantic-conventions/pull/2160) — 2025-04 合入，agent 作为一等公民
3. [OTel PR #2247: agent.node.* for agent-graph spans](https://github.com/open-telemetry/semantic-conventions/pull/2247) — 2025-05 合入，多 agent 图结构
4. [OTel Issue #2179: chat history on gen_ai spans and events](https://github.com/open-telemetry/semantic-conventions/issues/2179) — 讨论 input.messages 用 span attr vs span event
5. [Langfuse Data Model](https://langfuse.com/docs/observability/data-model) — observation-centric v4
6. [Langfuse 2025-08 Enhanced Observation Types](https://langfuse.com/changelog/2025-08-27-enhanced-observation-types) — 10 种 type 定型
7. [Langfuse Python DeepWiki: Observation Types](https://deepwiki.com/langfuse/langfuse-python/2.2-observation-types) — SDK 级字段实现
8. [OpenInference Semantic Conventions (Arize)](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md) — 10 种 span.kind + reserved attributes
9. [Arize AX openinference docs](https://docs.arize.com/docs/ax/observe/tracing/tracing-concepts/openinference-semantic-conventions) — 平台侧规范
10. [Generative Agents source: associative_memory.py](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/memory_structures/associative_memory.py) — ConceptNode 完整字段 + event/thought/chat 三分
11. [Generative Agents paper (Park et al. 2023)](https://arxiv.org/abs/2304.03442) — memory stream / reflection / retrieval
12. [HA Event Handling Guide](https://www.mintlify.com/home-assistant/core/guides/event-handling) — EventBus + state_changed + context(parent_id)
13. [HA developer docs: firing events](https://developers.home-assistant.io/docs/integration_events/) — integration 层事件规范
14. [Event Sourcing for Agents (Phoenix 2026)](https://understandingdata.com/posts/event-sourcing-agents/) — AgentEvent envelope + 5 大 category 分类 + snapshot 策略
15. [Event-Driven Architecture for AI Agent Systems (Zylos 2026-03)](https://zylos.ai/research/2026-03-02-event-driven-architecture-ai-agent-systems) — LangGraph/AutoGen/CrewAI 的事件模型对比
16. [Event-Sourced AI Agents Production Blueprint 2026 (AIStackInsights)](https://aistackinsights.ai/blog/event-sourced-ai-agent-architecture) — 企业落地经验
17. [ContextAgent NeurIPS 2025 (OpenReview)](https://openreview.net/pdf/8c61939b607693d9b13cc1df27793d844f3648f5.pdf) — 传感器 context + persona + 双阶段 proactive 决策
18. [ProAgent: On-Demand Sensory Contexts](https://koineu.com/en/posts/2025/12/2025-12-07-2512_06721/) — on-demand sensor 调用，与 ContextAgent 互补
19. [Apple: LLMs for Late Multimodal Sensor Fusion (NeurIPS 2025)](https://machinelearning.apple.com/research/multimodal-sensor-fusion) — LLM 作为 sensor fusion 的 late-stage decider
20. [Amazon Science: Hunches Deep Device Embeddings](https://www.amazon.science/blog/the-science-behind-hunches-deep-device-embeddings) — Alexa 1/4 交互已由设备状态 embedding 主动发起
21. [OTel PR #2881: invoke_agent beyond remote agents](https://github.com/open-telemetry/semantic-conventions/pull/2881) — 2025-10，澄清本地 agent 也用 invoke_agent span
22. [Traceloop openllmetry #3515: gen_ai.prompt deprecated](https://github.com/traceloop/openllmetry/issues/3515) — 证实 gen_ai.prompt/completion 已被 input.messages/output.messages 替代
