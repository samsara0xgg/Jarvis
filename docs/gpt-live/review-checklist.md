# GPT-Live x Jarvis 审核清单

来源：官方文档全文，抓取日 2026-09-12，原文按页存于同目录：

| 文件 | 页面 |
|---|---|
| `live-intro.md` | https://developers.openai.com/api/docs/guides/live |
| `gpt-live-1-model.md` | https://developers.openai.com/api/docs/models/gpt-live-1 |
| `voice-websockets.md` | https://developers.openai.com/api/docs/guides/voice-websockets?api=live |
| `live-conversations.md` | https://developers.openai.com/api/docs/guides/live-conversations |
| `live-delegation.md` | https://developers.openai.com/api/docs/guides/live-delegation |
| `live-prompting.md` | https://developers.openai.com/api/docs/guides/live-prompting |
| `voice-server-controls.md` | https://developers.openai.com/api/docs/guides/voice-server-controls?api=live |
| `live-migration.md` | https://developers.openai.com/api/docs/guides/live-migration |
| `voice-latency-cost.md` | https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live |
| `live-api-reference.md` | https://developers.openai.com/api/reference/resources/live/* |

对照物：docs/gpt-live-integration-planning.md（基线 68e60b9）。
用法：Allen 交付某一阶段的实现时，逐条核对；每条给出 Live 事件/字段/官方原话。

---

## 0. 一句话结论

Live 是"连续音频流 + 模型自己决定何时说 + 应用通过三个 append 事件喂上下文 + 委托只给元数据"的模型。
它没有：回合边界事件、输出音频完成事件、远端停止/取消事件、委托任务文本、重连语义。
所有这些都必须在 Jarvis 侧用本地播放器状态、转录片段、task revision 和持久化补上。

---

## 1. 连接与握手（WebSocket，Jarvis 选定）

| 项 | 事实 |
|---|---|
| URL | `wss://api.openai.com/v1/live/sessions`，无 query 参数 |
| Auth | `Authorization: Bearer $OPENAI_API_KEY`（key 只能在 daemon 侧） |
| 第一条消息 | `session.start`，`session: {model, instructions, input, audio, delegation, store, client}` |
| 就绪信号 | `session.started`（含 resolved config + `session.id`），**之前不能发音频或命令** |
| SDK | `openai[realtime]` extra；`AsyncOpenAI().live.connect()`；`openai.types.live.session_config_param.SessionConfigParam`；`connection.session.input_audio.append(audio=...)`；`connection.session.thinking.append(...)`；`connection.session.close()` |
| REST 创建 | `POST /v1/live/sessions` **只用于 WebRTC**；WS 是带内 `session.start` 建会话 |
| Sideband | `/v1/live/sessions/{id}/attach`；Jarvis 自己持有主连接，**不需要** |
| event_id | 所有客户端事件可带，≤512 字符，ACK 里回作 `client_event_id` |

审核项：
- [ ] 本地 openai SDK 版本有 `client.live`（第一件事）
- [ ] 音频发送严格在 `session.started` 之后
- [ ] 每个会 ACK 的命令都带 `event_id`，并按 `client_event_id` 匹配

---

## 2. 音频

| 项 | 事实 |
|---|---|
| 格式 | `session.audio.format` 建会话时定死，**输入输出同一格式**，会话内不可改 |
| 选项 | `{"type":"audio/pcm","rate":24000}` 默认 / `{"type":"audio/pcm","rate":16000}` / `audio/pcmu` 8k / `audio/pcma` 8k |
| 编码 | base64 裸字节，无 WAV 头，mono s16le；PCM 块字节数必须为偶数 |
| 输入节奏 | 必须按真实采样速度连续送；"Piping an entire file at once does not simulate a live microphone" |
| 输入 ACK | `session.input_audio.append` **没有 ACK** |
| 输出 | `session.output_audio.delta.delta` → 按到达顺序入队播放 |
| 输出时间 | 主连接上 `start_ms/end_ms` 是 optional（reference）/ 指南说没有；**不能依赖** |
| 完成信号 | **没有 output-audio-done 事件**；后台响应完成 ≠ 说完；转录时间戳 ≠ 播放完成 |
| 重采样 | 应用自己做；改 format 不会转换字节 |
| 时间线推进 | 会话时间线由输入帧推进；"If frame progress stops, an acknowledgment can remain pending" |

审核项：
- [ ] 采样率决策已记录：16k（省一次输入重采样，输出也 16k）vs 24k（输出音质，输入 48→24）
- [ ] 输入路径是持续消费麦克风，不是 VAD 切完整 WAV 再提交
- [ ] Live 开启时 SenseVoice 不再把同一段语音提交为第二个后台请求（规划 §4.1）
- [ ] 偶数字节对齐（尾字节带到下一块）
- [ ] 本地静音期间**继续发静音帧**或用 `input_audio.mute`，不能单纯停发（否则 append ACK 挂起）
- [ ] 播放器有自己的队列游标；"speaking" 指示来自播放状态，不来自任何 Live 事件
- [ ] 没有第二个播放器（Provider 不另开输出流，规划 §2.1）

---

## 3. 事件总表

客户端→服务端（11）：
`session.start` `session.update` `session.input_audio.append` `session.input_audio.mute` `session.input_audio.unmute` `session.instructions.append` `session.thinking.append` `session.commentary.append` `response.item.create`* `response.create`* `session.close`
（* 仅 Responses 委托模式，Jarvis client 模式用不到）

服务端→客户端（主连接相关）：
`session.started` `session.updated` `session.input_audio.muted` `session.input_audio.unmuted` `session.instructions.appended` `session.thinking.appended` `session.commentary.appended` `session.output_audio.delta` `session.input_transcript.delta` `session.output_transcript.delta` `session.delegation.created` `response.event`* `session.usage.updated` `session.closed` `error` `info`
（其余 `transport.*` 为 SIP/sideband 专用）

**不存在的事件**（审核时如果实现里假设了它们，就是错的）：
turn-complete / item id / output-audio-done / delegation.cancel / response.cancel（Live 模式）/ 任何 pause-resume / 任何 reconnect-resume。

---

## 4. 上下文注入三件套

| 事件 | 用途 | ACK |
|---|---|---|
| `session.instructions.append` | 应用签发的行为指令；**唯一被官方写明 "can interrupt speech in progress"** | `session.instructions.appended` |
| `session.thinking.append` | 静默事实/进度，不立刻说 | `session.thinking.appended` |
| `session.commentary.append` | 要说出来的内容，**模型被训练成会改述** | `session.commentary.appended` |

共同规则：
- `content` 纯字符串 ≤ **500 tokens**
- `delegation_id` **必填**（key 必须存在），`null` = 会话级；非 null 必须是已知 client delegation
- ACK 在"帧进度到达估计注入点"后才回；带 `start_ms/end_ms`（估计范围）
- ACK **不证明**：模型消费了全部内容 / 说了 / 停了 / 外部动作完成
- 关会话时未 ACK 的 append 报 error
- 三者都不是隐私边界；secrets 不进
- 不把用户原话拼进 instructions

审核项：
- [ ] 每个 append 有 token 预算检查（500）
- [ ] `delegation_id` 显式传 `None`，不是省略
- [ ] 不用 ACK 当"已播报"或"已停止"的证据（规划 §8.3 heard ledger）
- [ ] UI 上下文变化用 `thinking.append(delegation_id=None)`，去重、合并快速变化、变化写成"现在是 X，之前是 Y"

---

## 5. 委托（client 模式）

`session.start.session.delegation = {"type": "client"}`（省略或 null 也是 client）。

`session.delegation.created`：
```json
{"type":"session.delegation.created","event_id":"...","offset_ms":1000,
 "delegation":{"id":"item_...","type":"delegation","target":"client"}}
```
- 只有元数据，**没有任务文本、没有音频、没有参数**
- ID 不透明（当前 `item_` 前缀），原样返回
- 可能在转录里完整句子出现之前就到
- 同一 delegation 可以多次 append（流式进度/结果）

官方 adapter 骨架（migration 页，Python）：
1. `event.type == "session.delegation.created" and event.delegation.target == "client"`
2. `context = read_context()`；None → 保留通知，不动手
3. `summary = await run_agent(context)`（≤500 tokens，已验证）
4. `if current_revision() != context.revision: return`（丢弃过期结果）
5. `connection.session.commentary.append(event_id=uuid, delegation_id=event.delegation.id, content=summary)`

官方额外要求：
- 调 adapter 前先在应用里 **claim** 这个 delegation（防重复投递起两次）
- 后台执行副作用前**再查一次** revision（adapter 的检查只防播报过期结果）
- "Keep operation IDs and task revisions separate from delegation IDs"
- 重试前先查原操作是否已发生（"a lost response should not cause a second booking"）
- commentary 只在动作真正成功后发（"Only send that result after the booking has actually succeeded"）
- 打断说话 ≠ 取消后台；取消请求 ≠ 取消成功

审核项（对应规划 §6）：
- [ ] receipt 以 `(provider_session_id, delegation_id)` 持久化，且 claim 是原子的
- [ ] 请求构建用转录片段 + 时间窗 + 当前任务 + Session Brief，不是"最后一句"
- [ ] 证据不足 → 挂起等待，不捏造
- [ ] 回传前 revision 检查；副作用前 revision 检查（两处）
- [ ] 进度走 thinking，结果走 commentary，失败如实（"That time is no longer available" 模式）
- [ ] 完整结果给 UI，不给 Live；不额外调模型把短结果改写成口语
- [ ] 不把 Jarvis 全部工具注册给 Live（client 模式本来就不注册）

---

## 6. 控制

| 用户意图 | Live 能做的 | Jarvis 必须做的 |
|---|---|---|
| 别说了 | `instructions.append("Stop speaking...", delegation_id=None)` | 本地 gain 0 / 停播 / 清队列 / 标记旧 generation；恢复时不倒旧队列 |
| 停止生成这个回答 | **无远端取消**；同上 instructions | 取消自己的后台任务 |
| 取消操作 | 无 | Action 取消流程，核对成功后才说 |
| 麦克风静音 | `input_audio.mute` → 等 `muted` | 继续发静音帧或接受 ACK 挂起 |
| 播放静音 | 无 | 本地 |
| 暂停 | **无 pause/resume**；软暂停 = mute + 本地停播 + instructions；硬暂停 = close + 重开 | 见 §7 |

官方推荐的播放控制顺序："temporarily mute or drop the output, discard locally queued audio, send the corrective instruction, and resume playback according to your application's recovery policy. Clear stale audio before resuming."

Moderation：有的结束会话，有的**只切断当前这句音频并发 `error`，会话不关**。不能把音频中断当断线，也不能把被切断的话标成已送达。

审核项（对应规划 §8）：
- [ ] "别说了"路径先动本地播放器，再发 instructions，两者独立可观测
- [ ] 恢复策略显式（后台完成不立即抢话，规划 §7.3）
- [ ] 收到 `error` 时不假设会话已死；区分 moderation 切音 vs 真错误
- [ ] mute 不误取消后台任务（规划 §12 最后一行）

---

## 7. 会话限制、生命周期、计费

限制：
- `instructions` ≤ 16,384 tokens；`input` ≤ 128 条 / 8,192 tokens；角色 `developer/user/assistant`，**无 `system`**；纯文本
- 上下文 128k；>90% 换替代引擎，带原始 instructions + ≤8,192 tokens 历史 → Session Brief 按 8k 内设计
- 并发会话：Tier1 25 / Tier2 50 / Tier3 200 / Tier4 300 / Tier5 500；Free 不支持
- `session.closed.reason: expired` = 有时长上限（数值未公布）

关闭：
1. 先注册 `session.closed` 监听
2. 发 `session.close`，停止提交新工作，**保持连接**直到 `session.closed`
3. 读 `usage.seconds` / `reason` / snapshot
4. 之后才释放传输和音频设备；示例超时 15 s，超时 = "incomplete finalization"
- reason 枚举：`close_requested` `expired` `content` `remote_hangup` `connection_lost`
- socket 关闭 ≠ 成功；没有 `session.closed` 则 usage 未确认

计费：
- $0.05/min 按秒，不进位（文档快照，部署前复核）
- **从 start 到 close 全程计费，包括双方沉默、mute、后台在跑**
- `session.usage.updated.usage.seconds` 累计快照，不能相加；`context_window.usage_ratio` 同事件
- WebRTC 建会话预扣 15 s（WS 不适用）
- 官方："Closing saves $0.05 per minute of idle voice time"

恢复（无 reconnect 语义）：
- 新会话 + `input` 里放 `developer` 消息（"Saved task: ... Result: ..."）+ `delegation: {type: client}`
- 或 `store: true` 的会话 fork（新 ID，30 天，ZDR 不可用）；MVP 不依赖（规划 §9 成立）
- 旧 session 的 delegation ID 全部作废

审核项（对应规划 §9 + §3.5）：
- [ ] 闲置阈值是配置项；超过则硬暂停（close），后台继续
- [ ] 后台完成事件可触发新会话 + 通知
- [ ] 每次 `session.closed` 的 `usage.seconds` 落库一次；断线则标 unconfirmed
- [ ] 新会话不重放旧语音；跨会话结果重新关联
- [ ] 不用 provider session ID 当任务身份

---

## 8. Prompt

官方模板固定标签（保留标签，改内容）：
```
You are [name], ...
Backchannel policy: Use moderate backchannels. ...
Interruption policy: Stop speaking when the user interrupts. Listen to what they say.
Delegation policy:
Backend tools:
- [capability]: [what the backend can do]
Delegate to the backend when:
- ...
Do not delegate to the backend when:
- ...
Delegate before giving an answer that depends on backend work.
Do not guess the result while waiting.
```
- 用目标语言写（Jarvis → 中文）
- 不加"用户说话时绝不出声"（会压掉 backchannel）
- 不放工具 schema、业务流程、权限规则
- 只列后台真有的能力
- 可选控制块按需加，不全抄
- "Stop talking" 和 "Cancel my booking" 是两件事，模型知道，后台要分别处理

审核项（对应规划 §7.1）：
- [ ] instructions 只含人格/语言/节奏/backchannel/打断/委托条件
- [ ] 委托条件包含：完整历史与原话、当前任务状态、动态信息、工具执行、复杂推理、改后台任务的修正
- [ ] 中文书写

---

## 9. 官方明确"不能依赖"清单（审核时的红线）

1. append ACK ≠ 消费/说出/停止/动作完成
2. `start_ms/end_ms` ≠ 播放完成 ≠ 词级对齐 ≠ 回合边界
3. 转录 delta ≠ 完整回合；缺事件 ≠ 沉默
4. mute ≠ 停推理/停后台/停说话
5. commentary 会被改述；instructions 请求的措辞不保证原样
6. usage 快照不能相加
7. socket 关闭 ≠ 成功关闭
8. 新连接 ≠ 恢复旧会话或其待办
9. 后台响应完成 ≠ 用户听到
10. 口头拒绝 ≠ 工具没跑（"A spoken refusal does not prevent a tool from running"）
11. Live 对话历史可被压缩，"it is not your booking record"
12. 模型说的 ≠ 已验证事实（规划 §7.3 已有）

---

## 10. 按阶段的最小验收对照

阶段 A（最薄闭环）
- [ ] `session.started` 前无音频；格式决策；连续帧；偶数字节
- [ ] 输出直接入本地播放器，单一设备 owner
- [ ] 转录 delta 按 speaker 独立累积，保留 `start_ms/end_ms`，可重分组
- [ ] mute/unmute 走 Live 事件 + 等 ACK；本地停播独立
- [ ] 关闭流程按 §7；`usage.seconds` 落库
- [ ] 实测：mute 60 s 看 `usage.seconds` 是否增长（预期增长）
- [ ] 实测：停发帧 N 秒后 append ACK 是否挂起、会话是否断
- [ ] 实测：中文/中英混说/人名；耳机 vs 无 AEC 扬声器 vs 有 AEC 扬声器分别记录
- [ ] 没有旧链双重回答/双重播放

阶段 B（委托闭环）
- [ ] receipt + claim 原子
- [ ] 请求构建不依赖不存在的"最终转录事件"
- [ ] thinking 进度 / commentary 结果 / 失败如实
- [ ] 重送同一 delegation 不重起任务
- [ ] 转录片段不逐段触发回答
- [ ] Session Brief ≤8k，developer 角色进 `input`

阶段 C（一致性与恢复）
- [ ] revision 两处检查（回传前、副作用前）
- [ ] 旧结果已注入后收到修正：发新事实 + 必要时本地停播清队列（规划 §6.3，实测项）
- [ ] 权限拒绝 → 工具不跑 → 状态正确 → instructions 引导拒绝
- [ ] 断线：保留最后 usage，标 unconfirmed，后台继续，新会话重关联
- [ ] 不明确执行结果不盲重试

阶段 D（统一 Provider）
- [ ] 能力声明区分：本地硬停播 vs 远端 instructions 引导；Session Brief 新开 vs fork；支持存储 vs 启用存储
- [ ] SpeakingStarted/Stopped 若有，标注来源为本地播放状态

---

## 11. 对规划文档的修正/补充

| 规划位置 | 补充 |
|---|---|
| §3.1 | 加：REST 创建只 WebRTC；WS 带内 `session.start`；`info` 事件存在 |
| §3.3 | 加：instructions 16,384；input 128/8,192 无 system；128k + 90% 压缩到 8,192；**帧停止则 ACK 挂起** |
| §3.4 | 加：`instructions.append` 是唯一能打断当前说话的远端命令；moderation 可切音不关会话 |
| §3.5 | 加：mute 不停表；关会话省钱；官方明确推荐 ambient agent 长任务期间关会话 |
| §3.6 | 加：主连接 `output_audio.delta` 的 `start_ms/end_ms` reference 标 optional，仍不可依赖 |
| §5.2 | "请求停止发声"的真实能力 = instructions.append + 本地；不存在远端 cancel |
| §6.1 | 官方 adapter 骨架与之对应；加 claim 步骤 |
| §9 | 官方成本页直接背书"关会话 + developer 消息重开" |
| §14 | 新增实测项：mute 计费、停帧 ACK、`expired` 时长上限 |

---

## 12. 待实测（文档没写或写了"test it"）

- 停发输入帧多久会断线 / `expired` 的实际上限
- 中文 voice 表现（voice 表都是英/葡语风格，语言靠 prompt）
- 本地停播后模型上下文与用户实际听到的偏差如何恢复
- 旧 commentary 已注入后修正的误播率
- 委托事件 vs 转录到达顺序
- 各设备 profile 的回声与插话
