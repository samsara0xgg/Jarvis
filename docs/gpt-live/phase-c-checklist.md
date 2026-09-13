# GPT-Live 阶段 C 清单

整理日期：2026-09-13。基线 main `b63c3fd`。
状态：规划清单，尚未开工。实施前先落 ADR（第 1 条），再按
`docs/goals/` 的 goal card 流程启动。

对照：`../gpt-live-integration-planning.md` §11 阶段 C、§6、§9；
`../adr/0016-*.md` §4 Non-goals；`lifecycle-as-built.md`；
`review-checklist.md`。

## 1. 阶段 C 在做什么

阶段 A 让 Live 能听能说；阶段 B 让 Live 能"问后台"，但后台只许查不许做
（ADR-0016 D6 只读工具视图）。阶段 C 让 Live 能"让后台做事"，并且安全：

- 后台做有后果的事之前必须问 Allen，Allen 口头说"好/确认"才做，说"不要"
  就不做；模型自己说什么都不算数。
- Allen 中途改主意，旧的问题作废，晚到的"好"不会误触旧操作。
- 网线断了、daemon 重启了，该做的事不做两遍，没做成的会告诉 Allen，
  不自动重试。
- 上次会话没听到的结果，下次连上补给 Allen。

ADR-0012 的确认流已经提供了最难的部分：`confirm_required` →
`confirmation.requested` → PendingConfirmations 槽位 → 精确语法匹配 →
`_handle_confirmation_accepted` 铸 AuthorizationLease → 完整 pre-action
gate。新槽位自动取代旧槽位，过期/已答的槽位让语法失效。所以规划 §6 担心的
"语义 revision"对副作用操作已由槽位取代机制提供，阶段 C 不另造 revision 表。

真正的缺口：今天"好"只能从 ASR → `_handle_utterance` 进来。Live 模式下
Allen 的"好"是 `session.input_transcript.delta`，模型按 prompt 不会为一个
字的回答发 delegation，没有任何东西把它送进后台。

## 2. 改动清单

| # | 文件 | 改什么 | 依据 |
|---|---|---|---|
| 1 | `docs/adr/0016-*.md`（amend）或新 ADR | D2 加例外：Live 用户转录行在"Live 发起的确认槽位存活期间"命中 confirm grammar 时，可不经 delegation 直接提交为 turn；D6 改为按配置白名单开放可变工具；新增"未送达结果跨会话补投"、"usage 落 Event Log"两条决策；D5 晚到规则改（见 §3.1）。写之前读 ADR-0012 §10.6 open findings | CLAUDE.md：跨层合同先落 ADR |
| 2 | `config/jarvis.yaml` `realtime.gpt_live` | 加 `mutating_tools: [write_file]` 白名单（默认只此一项）；prompt `Backend tools` 改成"能查，也能写文件但要 Allen 口头确认"；`Do not delegate` 加"Allen 回答确认问题（好/确认/不要）时不要委托、不要替他答" | 官方模板：只列后台真有的能力；确认回答必须留给语法 |
| 3 | `jarvis/runtime/__init__.py` `drive_turn`（:2442） | `channel == "gpt_live"` 时不再固定替换成 `ReadOnlyToolRegistry`，改为"只读 + 白名单"视图 | 让 `write_file` 走正常 `confirm_required` |
| 4 | `jarvis/execution/tools.py` `ReadOnlyToolRegistry`（:6499） | 改成 `LiveToolRegistry(inner, allow)`，`_visible = read_only or name in allow`，仍排除 `cancel_action` | 一处改动，过滤逻辑不动 |
| 5 | `jarvis/surface/voice_live.py` 确认回答触发 | 用户行在 `_ROW_GAP_S` 关闭时（现有 flusher 处 :1139），若 `pending_confirmation()` 报告有存活槽位且行文本命中 grammar，则经 inbox 提交（`request_id` = 行 record_id，channel gpt_live）；未命中什么都不做 | 复用 `_handle_utterance` 钩子，LLM 不被调用；D2 其余不变 |
| 6 | `voice_live.py` `_Pending` | 去掉"必须有 delegation_id"的假设：确认 turn 的结果（已写入/已取消/失败）走同一套 `_await_result` → `_deliver`，`delegation_id=None` | 现 `_deliver` 只认委托关联的 pending |
| 7 | `voice_live.py` 送达问句 | 后台以 `confirm_required` 结束的 turn，答案即问句；按现有 commentary 送，先发 thinking："后台在等 Allen 口头确认，只有他明确说好才执行，不要替他答，也不要说已经做了" | 官方：commentary 会改述；槽位和 UI 是真值 |
| 8 | `voice_live.py` 会话启动补投 | `session.started` 后调 `undelivered_results()`，把上个会话未送达的（含 `daemon_restart`/`connection_lost` 失败）作 `thinking(delegation_id=None)` 注入，再 `mark_delivered()` | ADR-0016 §3 "cross-session delivery 未提供"；`_close_delegations` 在 teardown cancel 任务，结果现在会丢 |
| 9 | `voice_live.py` `session.closed`（:741） | 调 `record_usage(session_id, usage.seconds, reason)` | 阶段 A 遗留；成本页要求每会话记一次 |
| 10 | `jarvis/runtime/inherent_loop.py` `_LiveBackend` | 加 `pending_confirmation()`（读 PendingConfirmations 投影）、`undelivered_results()`、`mark_delivered(turn_id)`、`record_usage(...)`；四个 callable 收成一个 `LiveBackend` Protocol | D9 分层：L5 不 import state |
| 11 | `jarvis/state/event_log.py` | 注册 `live.result_delivered {turn_id, session_id, kind}`、`live.session_usage {session_id, seconds, reason}` | 送达标记必须持久，不能在 `_LiveRun` 内存 |
| 12 | `docs/live-burn-2026-09-XX-gpt-live-phase-c.md` | 验收证据 | ADR Definition of Done 模式 |
| 13 | `voice_live.py` `_settle_request`（:943-973） | 就绪判断从到达墙钟改为会话时间线：已收到 `end_ms ≥ offset_ms` 的用户片段，且其后 600 ms 时间线内无新片段；2 s 上限保留兜底。先用 daemon 日志里的 `offset_ms`/`window`/片段 `start_ms,end_ms` 统计分布，验证 600 ms / 2 s 有没有切过请求 | 官方："Do not infer silence from a missing event… network delivery can be uneven"；"tune any gap timeout against recorded conversations" |
| 14 | `voice_live.py` `_recv_loop`（:742） | `session.closed` 缺 `usage` 时不 KeyError（`.get` + 标 usage 未确认），避免正常关闭记成 `recv_failed` | verifier 发现 |
| 15 | `voice_live.py` `_send_loop`（:1096-1106） | 加与 `_send_json` 对称的 try/except：`ws.send` 失败记 `last_error` 并触发 `recv_failed` 路径，而不是静默结束 | verifier 发现 |

不需要改：`config/confirm_grammar.yaml`（精确字面量，"好。"尾标点已允许）、
`authorized_dispatch_outbox`、`response_run.reconcile_open_responses`、
`docs/spec.html`（ADR-0012 已声明 spec 在确认流上 silence，ADR-0016 已
amend §3.6.5）。

## 3. 开工前 Allen 要定的事

### 3.1 D5 晚到规则

现状：90 s 后发"还没拿到结果"，之后到的 answered 只作 thinking，永不说出
（`voice_live.py:922-941`）；D10 又让本地链不读 gpt_live turn。所以 >90 s
的任务结果没有任何嘴会说。建议改为：90 s 仍发"还没拿到结果"；最终答案只要
没被取代、会话还开着就作 commentary；会话关了进未送达集合，由第 8 条送。

### 3.2 后台任务完成 → 触发 Live（Allen 2026-09-13 的提法）

官方成本页背书："use a backend completion event to start a new session
and notify the user that the result is ready"。两个方案：

| | A：完成即开 Live | B：本地链先说，Live 按需开 |
|---|---|---|
| 听到通知 | Live 的声音，连上 1–2 s 后出声 | MiniMax，立刻 |
| 追问 | 直接说，麦克风已开 | 唤醒词/PTT，Live 起来后 brief 里已有结果 |
| 成本 | 每次通知一个会话 | 不理就零成本 |
| 麦克风 | 无提示打开并上传，靠提示音 | 不开 |
| 要改 | daemon 侧 `start()` 调用路径 + D10 改为"送达时开着的嘴发声" + 启动即播报（greet 模式：`instructions.append` + `commentary.append("Begin now")`）+ attention_policy 复用 + 通知型短 idle | 只要第 8/11 条 |

审核 session 建议先 B；A 在 Live 音色可接受后再上，改动照样成立。
注意 D6 只读视图意味着 Live 今天发不起长任务（`spawn_worker` 非
read_only），trigger 场景的任务几乎都来自文本/本地链。

### 3.3 扬声器静音期间的结果

现在照发 commentary、模型照读、进 gain 0 的播放器（as-built D4 有意）。
按秒计费 + 上下文消耗，是否改成静音时降为 thinking 或缓存到取消静音——
Allen 定。

### 3.4 请求横跨两条 memory.db 行

`record_id` 取 `unflushed[0]`（:892），前半行会作为历史重复进 prompt。
是否合并为一行或都排除——Allen 定；影响 prompt 而非行为。

## 4. 验收（对照规划 §11 阶段 C）

每条以 daemon 日志行 / Event Log 事件 / memory.db 行 / 实际播放为证据，
不以测试套件全绿代替。

| 规划要求 | 证据 |
|---|---|
| 改要求后旧结果不作为当前答案输出 | D5 已有；加：旧槽位被新 `confirmation.requested` 取代后，晚到的"好"落到已失效槽位，语法不触发（ADR-0012 C3），日志无 `confirmation.accepted` |
| 旧操作已发生时记录事实 | memory.db `jarvis` 行 + `action.*` 事件；下一 turn 的 LLM 提示里可见 |
| 拒绝权限不执行 | "不要" → `confirmation.rejected`，无 `action.dispatched` |
| 网络重连不重复动作 | 槽位 TTL 10 min 在投影里跨会话存活；新会话补投"还在等你确认"；Allen 再说好，只一个 `action.dispatched` |
| 不明确执行结果不盲重试 | 全链无自动重试；daemon 重启 → `daemon_restart` 失败 → 新会话听到"失败了"，无第二次 dispatch |
| 第 13 条 settle | 日志分布统计 + 一次网络抖动复现（人为延迟 >600 ms）不切请求 |
| 第 9/11 条 usage | 每次 `session.closed` 一条 `live.session_usage`，与 log 中 `usage.seconds` 一致 |
| 第 14/15 条 | 构造缺 `usage` 的 closed / 断 socket 时 `close_reason` 正确 |

软肋（设计如此，体验要看）：Live 转录把"好"写成"嗯好"或"好的呀"时语法
零泛化不命中，落回普通 turn，LLM 只能重新提议（新槽位再问一次），不会误执行。

## 5. 不在阶段 C

Provider 抽象 / `LocalChainedVoiceProvider`（阶段 D，且预期缩成配置开关
+ Protocol）、WebRTC、供应商存储与 fork、进度逐步转发（第 8 条只补投终态；
中间进度是 D5 之后的独立项）。
