# GPT-Live × Jarvis 生命周期（as built）

基线：main `b63c3fd`（2026-09-13）。所有行号指向该提交的
`jarvis/surface/voice_live.py`，除非另注文件。

来源：这张图先由审核 session 根据代码片段和 ADR-0016 as-built 画出，
再由一个只读 verifier（opus）对 28 条可判定断言逐条核对代码：
21 VERIFIED / 7 PARTLY / 0 WRONG。图中每一行都有 `path:line` 支撑；
verifier 未核的部分列在文末。

配套：`review-checklist.md`（官方文档提炼的审核清单）、
`phase-c-checklist.md`（阶段 C 改动清单）、
`../gpt-live-integration-planning.md`、`../adr/0016-*.md`。

## 1. 时序图

```
 Allen            LiveVoice (L5)                         OpenAI Live (wss)            Jarvis 后台 (L2-L4)
  │                   │                                        │                            │
  │ Resonance 开关 ──►│ POST /inherent/controls {"live":"start"}  inherent_server.py:969     │
  │                   │ start() 拒绝：busy / no_single_ingress / missing_OPENAI_API_KEY  :466-476
  │                   │ （全仓唯一调用点；没有 daemon 侧触发）    │                            │
  │                   │ brief() ◄──────────────────────────────┼────────────────────────────┤ memory_db.brief_note
  │                   │   [:1500]，每条记录截 200 字 + "..."     │                            │  :178,208
  │                   │ connect wss://…/v1/live/sessions (Bearer)                            │
  │                   │ ► session.start  （裸 ws.send，无 event_id） :574-587                │
  │                   │   session = {model, instructions, input:[developer brief],          │
  │                   │     audio.format{audio/pcm, rate}, audio.output.voice,               │
  │                   │     delegation{type:client}, store:false}   ← 正好七项                │
  │                   │ ◄ session.started {session.id}  握手期其它事件静默跳过 :1252-1270     │
  │                   │ 之后才：订阅麦克风(:599) → 起 reader/writer 线程(:620) → _apply_mutes(:495)
  │                   │ 本地链 tts_pipe.set_output_gain(0.0)  inherent_loop.py:4357          │
  │                   │                                        │                            │
  │══ 持续 ═══════════│ ► session.input_audio.append  ~100 ms 块，字节数 ×2 保证偶数 :1096-1106
  │                   │   麦克风静音时照发零样本帧(:1179-1189)；另发 session.input_audio.mute/unmute
  │                   │   但 fire-and-forget，不等 .muted/.unmuted (:516-525)               │
  │                   │ ◄ session.usage.updated → run.usage_s，只进 status()，不落库 :728     │
  │                   │                                        │                            │
  │ 说话 ────────────►│ ◄ session.input_transcript.delta → 字幕广播 + user_fragments + user_row :769-799
  │                   │   同说话人停 1.5 s → record("allen") :1139  ──────────────────────►│ memory.db
  │ 闲聊 ◄────────────│ ◄ session.output_audio.delta → b64 → pcm16→float32 → play 队列 → AudioStreamPlayer :723,1191
  │                   │ ◄ session.output_transcript.delta → assistant_row，播完且停 1.5 s 才 record("jarvis_live") :1141
  │                   │                                        │                            │
  │ "查一下…" ───────►│ ◄ session.delegation.created {offset_ms, delegation.id}             │
  │                   │ _on_delegation :805-859                │                            │
  │                   │   已在 pending 的 id → 只打日志         │                            │
  │                   │   更早的且 foreground 且 state∈{settling,submitted} → foreground=False（已答的不动）
  │                   │   若被顶掉的那个已 submitted → prior_request = 它的请求文本            │
  │                   │   window_start = max(上次委托 offset, anchor)  :1285-1309             │
  │                   │     anchor = 用户最近一串连续片段（间隔≤1.5 s）之前最后一条助手 end_ms，
  │                   │              没有则 offset−10 s；串只在 offset−末片段 end ≤10 s 时回溯  │
  │                   │ _settle_request :943-973               │                            │
  │                   │   窗口用 start_ms：window_start < start_ms ≤ offset+600 ms            │
  │                   │   停顿判据 = 墙钟到达时间 ≥600 ms（不是 end_ms）；上限 2 s              │
  │                   │   空窗口 → dropped + thinking "请让用户再说一遍" :879                  │
  │                   │ record("allen", 未 flush 的行, record_id) :905 ──────────────────►│ memory.db
  │                   │ delegate(带"此前请求"前缀的 request, delegation_id, session_id, record_id)
  │                   │   ─────────────────────────────────────┼───────────────────────────►│ submit_text_once
  │                   │   ◄────────── turn_id（重放同 key → 同 turn_id，不再 append）◄────────┤ inbox:230-247
  │                   │ ► session.thinking.append "正在查：…" (:915) ◄ .appended → _on_ack 只记日志
  │                   │                                        │                            │
  │ 继续聊 ◄──────────┼──── 全双工 ────                         │   surface.user_intent{channel:gpt_live, record_id}
  │                   │                                        │   drive_turn: ReadOnlyToolRegistry  runtime/__init__.py:2442
  │                   │                                        │   跳过自身 memory 写，record_id 作 exclude_id :2353
  │                   │                                        │   D8: _TTS_SILENT_CHANNELS ∋ gpt_live，tts_watcher 丢 open/chunk/emitted
  │                   │                                        │       commentary run 不开  inherent_loop.py:243,1513
  │                   │                                        │   → surface.response_emitted{phase:final} + response.completed
  │                   │ ◄── deliver(turn_id) ──────────────────┼────────────────────────────┤ bus: _LIVE_TERMINAL_TYPES 含
  │                   │   唤醒后 15×0.2 s 重读；兜底 5 s 轮询 :980-1005                       │  response_emitted + 3 个 terminal
  │                   │   lookup_result: phase==final → answered；failed/cancelled/turn.failed → failed  :4064-4109
  │                   │ _deliver :1007-1031                    │                            │
  │                   │   run/epoch/session 不符 → "kept in memory"                          │
  │                   │   foreground=False → 丢弃（turn 照跑、UI 照显、本地链也不读）           │
  │                   │   扬声器静音 → 照发 commentary（无降级分支）                            │
  │                   │ ► session.commentary.append  _speech_cut ≤300 字句末，否则"结果太长" :936,1317
  │ 听到 ◄────────────│ ◄ output_audio.delta / output_transcript.delta → record("jarvis_live")
  │ Resonance 看全文 ◄┼────────────────────────────────────────────────────────────────────┤ 响应流
  │                   │                                        │                            │
  ├─ 分支 ────────────┼────────────────────────────────────────┼────────────────────────────┤
  │ 90 s 无结果       │ ► commentary "还没拿到结果"，之后无限等；迟到 answered → thinking；迟到 failed → 仍 commentary :922-941
  │ 失败/任务抛异常   │ ► commentary "查询失败" :861-873        │                            │
  │ append 被拒       │ ◄ error{client_event_id} → 弹 run.appends；message 含 "token" 且未重试 → 半预算重发一次 :1056-1078
  │ "别说了"          │ 无处理：无转写匹配、无 flush、无 wire 命令；只在 prompt 第 62 行         │
  │ 扬声器静音按钮    │ player.set_gain(0) :527-531             │                            │
  │ info / *.muted / *.appended / *.updated │ → _on_ack 记日志 :752       未知 type → debug 日志，不刷新活动 :754
  │                   │                                        │                            │
  ├─ 结束 ────────────┼────────────────────────────────────────┼────────────────────────────┤
  │ 5 个自动停止：idle / max_session / 服务端 session.closed / connection_lost / recv_failed  + user / daemon_shutdown
  │ last_activity 刷新点：output_audio.delta(:724)、任一 transcript(:771)、speaking 上升沿(:1135)、有 submitted 未超时委托(:1148)
  │   ⚠ 麦克风帧本身不刷新 → idle_close_s 内无转写无音频即挂断    │                            │
  │                   │ _teardown :624-699                     │                            │
  │                   │   reader_stop → mic_muted → cancel/await 委托任务（开放的只打日志）→ flush 两行
  │                   │   → 未收过 closed 才 ► session.close → 等 close_timeout_s              │
  │                   │ ◄ session.closed{reason, usage}（由 _recv_loop 写 server_reason/usage_final :741-751）
  │                   │   → ws.close → 停线程 → player.flush/close → state=idle → 本地链恢复增益 → broadcast
  │                   │ 关闭后到的结果：无人送达、无 undelivered 标记，只在 Event Log + memory.db `jarvis` 行
  │                   │ 无 reconnect 代码；connection_lost 就结束                              │
```

## 2. 线路事件

代码实际发出（7 个）：

| type | 位置 | 备注 |
|---|---|---|
| `session.start` | :575 `_connect` | 裸 `ws.send`，无 `event_id` |
| `session.input_audio.append` | :1103-1106 `_send_loop` | 裸 `ws.send`，无 `event_id` |
| `session.input_audio.mute` | :524-525, :1220 | 经 `_send_json`，带 `event_id`，不等 ACK |
| `session.input_audio.unmute` | :524-525 | 同上 |
| `session.close` | :633 `_teardown` | 同上 |
| `session.commentary.append` | :1036-1040 `_send_append`，重发 :1068-1072 | 调用点 :819,870,928,934,941 |
| `session.thinking.append` | 同上 | 调用点 :884,916,939 |

未发出：`session.instructions.append`、`session.update`、`response.item.create`、
`response.create`（后两者只属 Responses 委托模式）。

recv 循环分派：

| kind | 位置 | 处理 |
|---|---|---|
| `session.output_audio.delta` | :723-725 | 刷新 `last_activity`，解码入 `run.play` |
| `session.input_transcript.delta` / `session.output_transcript.delta` | :726-727 → :769-799 | fragment + row buffer + 字幕广播 |
| `session.usage.updated` | :728-729 | 写 `run.usage_s` |
| `session.delegation.created` | :730-731 → :805-859 | 注册 + 起任务 |
| `error` | :732-740 | `last_error` + warning + `_on_append_error` + 广播 |
| `session.closed` | :741-751 | final usage、`server_reason`、`closed.set()`、自动停止 |
| `info` 及任何以 `muted`/`appended`/`updated` 结尾的 type | :752-753 → :1044-1054 | 按 `client_event_id` 弹 `run.appends`，只记日志 |
| 其它未知 type | :754-755 | debug 日志，不改状态，不刷新活动 |
| 握手期 `session.started` / `error` | :1252-1270 | 返回 `session["id"]` / 抛 `RuntimeError` |

Live 侧不存在的事件（实现里若假设了就是错）：回合结束、输出音频播完、
远端 stop/cancel、delegation 取消、pause/resume、重连恢复。

## 3. 往 Live 注入信息的方式与上限

| 方式 | 何时 | 模型怎么用 | 上限 | Jarvis 用法 |
|---|---|---|---|---|
| `session.start.session.instructions` | 建会话 | 行为/人格/委托规则，压缩后保留 | ≤16,384 tokens；会话内不可改 | `config/jarvis.yaml` `realtime.gpt_live.instructions` |
| `session.start.session.input` | 建会话 | 当作已发生的对话历史 | ≤128 条、≤8,192 tokens、只有 `developer/user/assistant`、纯文本、无 `system` | 一条 developer 消息 = brief（≤1500 字，每条记录 ≤200 字） |
| `session.instructions.append` | 会话中 | 追加行为指令；唯一能打断正在说的话；累积不过期 | `content` ≤500 tokens；`delegation_id` 必填（null = 会话级） | 不用（hush 实测模型静了 20 s，d6ea9d1 删除） |
| `session.thinking.append` | 会话中 | 静默事实，被问到才用 | 同上 | 提交时"正在查…"；空窗口提示；迟到的 answered |
| `session.commentary.append` | 会话中 | 要说出来的内容，模型会改述 | 同上 | 结果（≤300 字）、失败、超时、无后台 |
| `session.update` | 会话中 | 只能改 `delegation.responses.*` | client 模式无可改字段 | 不用 |

三个 append 的共性：ACK 要等帧进度到达注入点（帧停则挂起，故静音仍发帧）；
ACK 只证明收下；thinking 不是隐私边界（被顶掉的"明天"作为 thinking 曾被
当"后天"说出，故现在被顶掉的结果不发）；不接图片/音频/大 JSON/Markdown。

其它硬限制：上下文 128k，超 90% 同会话换引擎只带 instructions + ≤8,192
tokens 历史；`event_id` ≤512 字符；音频格式/voice/委托模式建会话时定死；
并发会话按 tier；计费 `session.started`→`session.closed` 全程按秒，mute 不停表。

## 4. 图外行为（verifier 发现）

1. 扬声器静音期间结果照样进 commentary、模型照读、进 gain 0 的播放器——
   听不到但计费且消耗上下文（:527-531, :1007-1031）。as-built ADR-0016 D4
   有意如此；结合按秒计费值得复议。
2. 一次请求可能横跨两条 memory.db 行：flusher 已写前半时 `record_id` 取
   `unflushed[0]`（:892），prompt note 只排除后半（runtime/__init__.py:2483）。
3. `_send_loop` 无异常处理（:1096-1106）：`ws.send` 抛异常麦克风发送任务
   静默结束。
4. `session.closed` 缺 `usage` 会 KeyError（:742）→ 通用 except → 正常关闭
   记成 `recv_failed`（:763-767）。
5. 被顶掉的委托仍完整执行、写 Event Log 与 memory.db、Resonance 照显，只在
   `_deliver` 丢弃；D8 使本地链也不读，因此只能在界面看到。
6. `run.notice` 是死字段（唯一赋值 :850 为 None）。
7. 同 `(session_id, delegation_id)` 不同文本重放 → `PayloadConflictError`
   （input_submission_inbox.py:234）→ Live 侧表现为 `FAILED_COMMENTARY`。
8. 麦克风订阅刻意延后到 `session.started` 后以丢弃陈旧 ring 帧（:592-601）；
   0.5 s 输入积压 drop-oldest 计数 `dropped_input_events`（:1108-1123）；
   `hearing` 1.5 s 保持（:781,1131）；`_apply_mutes` 开会话时继承当前开关
   （:1214-1221）。

## 5. verifier 未核对

未做 live run；未审 `AudioStreamPlayer`（voice_tts.py）内部 gain/ring/
`bytes_pending` 语义；未审 `voice_audio.AudioSubscription` 溢出/丢帧语义；
未审 Resonance 客户端如何调用 `/inherent/controls`。
