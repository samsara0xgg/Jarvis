# Jarvis 完成度审计 — Phase-2 checkpoint(2026-08-26)

**HEAD**: `a0f6264` · **审计方式**: 15-agent 对抗式 workflow(每单元 assessor + 独立 deflator 逐条 refute,另加 burn-coverage 盘点;~1.9M tokens,665 tool calls),证据源 = HEAD 代码树 + 生产 `~/.jarvis/mac_events.db`(1203 行,逐行复核)+ 三份 committed burn log。

**口径**:一个组件算数,必须 built **AND** wired **AND** live-exercised。assessor 先按此评,deflator 再对每条 claim 做 grep/DB 级 refute。**以对抗压分后的数字为准**。8-24 基线同样是压分口径,但本轮挖得更深(生产 DB 折叠、逐字段消费者 grep、daemon-vs-CLI 路径区分),所以负 delta 多数是"尺子更硬",不全是代码回退——真回退只有一条,见 delta 节。

---

## 1. 总表

| 层 | 8-24 基线(压分) | 8-26 评估 | **8-26 压分修正** | Δ |
|---|---|---|---|---|
| L1 constitution | 15 | 20 | **13** | −2 |
| L2 state | 36 | 58 | **47** | +11 |
| L3 decision | 50 | 68 | **53** | +3 |
| L4 execution | 46 | 65 | **54** | +8 |
| L5 surface | 33 | 42 | **30** | −3 |
| L6 deployment | 46 | 51 | **36** | −10 |
| runtime 组合根 | 65 | 70.5 | **58** | −7 |

简单平均 **42**。没有任何一个单元达到 70% exit bar;最接近的是 runtime(58)和 L4(54),最低是 L1(13)。

逐层一句话:

- **L1 (13)** — constitution 模块仍 100% import-dead(零生产 importer;唯一 unit test 被 5ef27d1 连带删除;2026-05-17 后未动);全部真实进展在 L3 `policy.py`,且 10 个 EffectivePolicy 字段里只有 `confirmation_threshold` 能改变结果、只对一个工具生效。
- **L2 (47)** — spine 是真的且在生产 DB 上复核(schema v1、registry 46、triggers、1203 行 actor 全回填);但生产 log 三个月只有 1 个 task、1 条 Postcondition claim、0 corrections、0 confirmations,RecentTrace 每 turn 折叠却零读者,Memory/Session/DriftWatch/freshness 仍为 0-stub。
- **L3 (53)** — gate→dispatch→interpret→pre-emit 的 spine 真实可审计,ADR-0012 lease/consent 是真代码真事件;但按 spec.html 量(§3.4.4 13-key packet、§13.1 8 检查、§12 attention、§17 17 patterns)只有一半,且旗舰 consent→execution 行从未 live 成功过。
- **L4 (54)** — 14-tool registry、caller 强制、SSRF guard、诚实的 burn 记录都是真的;但 §3.5.12 的 SandboxPolicy/ExecutionLease 拒绝面不存在,lease 检查全在 L3,§15 agent-run 机器 12 态实现 0,write_file 从未写出一个字节。
- **L5 (30)** — "ask_confirm 成为真实语音 surface" 只对 CLI one-shot 路径成立:daemon 传 `available_surfaces=frozenset()`,整张路由表在常驻态 inert;所有 8 月 burn 都走 `run_turn()`,daemon 侧(WS panel、TTS pipeline、wake listener、ASR、repo observer)零 live 证据。
- **L6 (36)** — Mac-domain 半边(bootstrap、launchd、process lock、supervisor sweep)真的在 Allen 真机上烧过;但生产 IOKit sleep/wake 从未 fire(生产 log 有史以来 0 条 `mac.sleeping`/`mac.awake`),federation 半边(cross-domain/MQTT/scheduler/RPi)全 0。
- **runtime (58)** — 同步 `run_turn` spine 真 live-verified,boot/finalizer/teardown 工程质量高;但装机后 `jarvis "..."` 默认走 forward path,该路径零 live 证据且有一个具体的关联 bug;daemon 半边(`--live-daemon` 套件)从未建成。

---

## 2. 与 8-24 审计的 delta

**真实前进**(commit 级证据,deflator 复核通过):

- L2 +11:schema v1 在生产 DB 原地迁移完成(`PRAGMA user_version=1`,actor/ingestion_node 落列,c0b8cc1);§6 projection 3/8 → 5/8 + PendingConfirmations;四个 correction 事件注册+折叠(554a56b);registry 41→46。
- L4 +8:工具 4 → 14(3fb37d2 / b68f0e3 / dbda5d7 / d1f6447),AuthorizationLease 九字段从无到有并在 burn 里 mint/validate/consume(ce2ec34),WorkerReport 字段不再丢弃(3672e26),SSRF guard 真实(tools.py:2787/2863)。
- L3 +3:confirm_required 从死路变真 ask(7de20f1),ask_confirm 可达且真的说话(notify.py:36),Tier 0 从空到 7 行双次 live 命中,lease 验证 v2 + 19 规则 consent grammar(ded19db)。

**负 delta 的构成**:

- 尺子更硬(不是回退):L5 −3 与 runtime −7 的主因是本轮发现了 8-24 没查到的结构性事实——daemon 路径 `available_surfaces=frozenset()`(inherent_loop.py:439)使全部已烧的 say/osascript 证据只认证 CLI 路径;forward mode 是装机默认却零 live 证据;L6 −10 主因是 §3.7.8 三条未满足 mandate(WAL 不 checkpoint、wake 跳过 dispatched-未-run orphan、reason 硬编码)+ 两列 provenance 结构性不可读(`_row_to_event` 不 SELECT 它们,event_log.py:1186/1212)。L1 −2 是 assessor 记给 L3 的 +5 被 refute(全树无任何 test 引用 effective_policy/autonomy_ceiling/confirmation_threshold)。
- **唯一真实回退**:`5ef27d1` retire tests/unit(−76 文件 −21019 行)。后果:process_lock/launchd/sleep_wake/load_env_file 失去唯一机械覆盖;J3/J10/K1/K2 四行的 skip message 指向已移出 repo 的 `~/Projects/jarvis-deprecated-tests/`,这四个不变量现在**零运行覆盖**。

---

## 3. 逐层缺口清单(全部 7 单元 <70%,均列)

压分幅度最大的条目排前;file:line 均经 deflator 独立复核。

### L1 constitution(13)

- `jarvis/constitution/__init__.py` 全模块零 import——grep 全树无 import 语句,event_log.py:29 / projections.py:38 / tools.py:47 / path_resolver.py:88 的命中全是"宣称不 import"的 docstring;没有任何 canary/gate/eval 能因宪法被违反而失败。
- EffectivePolicy 十字段里六个零消费者(仅 gates.py:736 一条 prose 注释);`autonomy_ceiling` 结构性 inert——14 个注册工具 13 个 ≤L2、唯一 L3 是 write_file(tools.py:5005),L3 ceiling 下 check 3 永远拒不了任何东西;`allowed_tool_surface` 按构造恒等(decision/__init__.py:3517-3524 与 policy.py:307 走同一 registry)。
- "铁律" boot validator(policy.py:328-359,runtime/__init__.py:836-842)是 tautology:pin 手写常量对自身;I7 floor 只是 docstring(policy.py:188-190),无任何机制阻止未来 preset 降门槛。
- Execution 轴被**反转而非拨动**:宪法 C4 明文主 LLM 默认拿不到 write_file,d1f6447 直接授予(tools.py:5004),无任何 ADR 对 C4 做 reconciliation——ADR-0012 里 grep "C4" 命中的全是 burn row 撞名。
- mode.transitioned / lens.enabled / override.applied 零注册;12 preset 只有 Collaborate 一个(policy.py:35);§11.4 组合器六输入只读一个(policy.py:223-234 返回九个硬编码字面量)。
- 无 canary pin `policy.py:124 _RISK_LADDER == constitution.AUTONOMY_LEVELS`——两份清单可静默漂移。
- prompts/jarvis_v1.md 与 2026-05-17(db8d029)byte-identical,无 policy/mode 插值;且静态 prompt 已在和机器 ask 对抗(10/20 prose 抢答,ADR-0012 §10.6 N)。
- C2-C6 的 live 证据不可复现:burn 脚本与 `burn_adr0012_v2_out.json` 均不在 repo 也不在盘上,只有 prose .md。

### L2 state(47)

- **Recent Trace 零读者**(压 75→30):packet.py:146 每 turn 写入,全树 `packet.recent_trace` 0 次读取;不像 status_board / pending_confirmation 有真渲染器。每 turn 全量折叠后即丢弃。
- **StatusBoard.open_actions 死代码且被复制**(压 78→60):projections.py:883/998 零消费者,而 L6 `sleep_wake.py:998 _open_actions` 用自己的 SQL + 自己的 dataclass 重新实现同一 belief——L2-projection 规则要防的正是这种双份独立漂移。
- correction 通路 wired 未 live-exercised:生产 log 0 条 refuted/limited/superseded/accepted;`claim.accepted` 无任何 emitter;唯一 live refutes 覆盖(L5 行)当前是红的。全 log 历史只出现过 1 条 Postcondition claim(2026 年 5 月)——§8.8 完成判定分支三个月只 fire 过一次。
- evidence freshness §3.3.3 全缺:`stale_warning` 全树 0 hits;freshness_ms/observed_at 是注册了没人读的 optional key;无 per-domain TTL ladder。
- §7 Session(session_id/active_task/task.activated)、§9 Memory(schema/promotion/authority ladder)、Drift Watch:零代码。ADR-0013 承接。
- `make_snapshot` = `rebuild_projections` 全量 refold(projections.py:1516-1522),无 high-water mark;§8.9 required-claim 模板表与 `required_for_completion` 字段均不存在,§8.8 规则无法按 spec 实现。
- registry "single source of truth" 弱化三处:emit_event 接受任意未声明 payload key(event_log.py:1054);§5.4 一致性规则对 §6 提到的 ~10 个类型不成立;缺的 `evidence_semantics` 恰是 §5.3 定为强制的字段。

### L3 decision(53)

- **Situation Packet vs §3.4.4**(压 55→38):13 个 canonical key 只有 ~2.5 个;四条 system note 是 prose 字符串、以 `role:"user"` 注入(LLM 把 runtime 状态当用户发言);§10.5 位置倒置是每 turn 的 prefix-cache 真实代价;PendingConfirmations 单槽 vs spec 的 `ConfirmationRequest[]`。
- **Pre-action Gate 4/8**(压 90→72):§13.1 八项里 active-task 绑定、workdir scope(落在 L4 path_resolver)、precondition evidence 三项完全没有;gate.evaluated 不带 policy 身份,事后无法审计哪个 policy 授权了历史 action。
- **确认流窄于分数**(压 78→58):consent→effect 端到端从未 live 成功(C1 红 8/8);ask 只 arm ~50%(五个绿行靠每行最多 4 次 retry);范围 = 1 工具,I7 的 spec 清单(删除/发送/push/merge/rebase/kill/install/secret)1/9;`granted_by:"allen"` 是硬编码字符串、无 speaker 身份;`confirmation.accepted` 链到**全局最新**的 request 而非被回答的槽。
- **Attention Policy 是 4-branch router**(压 35→20):§12.2 四阶段有三个不存在;§12.3 15 字段 0、§12.5 硬门 0、反馈调整 0、§12.6 per-mode 默认 0;§12.5 第一条硬门(risk high → ask_confirm)恰恰是 policy 永远不会返回的通道(finalize override,gates.py:829-836)。
- **Pre-emit 只盖 task-status 象限**(压 75→55):关键词 4/9;`active_subject_ref is None` 无条件放行(gates.py:738-747)——正是 write_file 提案的形状;六个 claim 类里 device result / memory write / mutable state 结构性在门外。
- Memory Write Decision §3.4.14 = 0(全树无 memory.proposed/observation.added emitter)。
- 证据折旧:43-scenario 全矩阵停在 dcac425(HEAD 前 20 commits、L3 相关 11 commits 之前);HEAD 的新代码只有 8+5 行单日定向 burn。

### L4 execution(54)

- **ToolDefinition 三字段 decl-only**(压 62→46):`domain` / `read_only` / `post_action_check` 全树零 runtime 读者;surface_for 只按 name+risk_level 过滤。
- **post_action_check 无消费者**(压 60→28):所有 grep 命中是 docstring/字段声明/单一构造点;verify_diff handler 硬编码自己的 predicate 和 `_VERIFY_COMMAND_TIMEOUT_S`(tools.py:1327),声明的 timeout_ms=600000 没人读——§3.5.7 recipe 引擎不存在,现有的是一个 bespoke 双槽 handler 加装饰性声明。
- **lease 的 L4 半边 = 0**(压 70→50):§3.5.5 明文把"lease 过期/target 越界"分给 L4 runtime;`dispatch`(tools.py:4129-4249)零 lease 检查,write_file_handler docstring 自认不查。
- **SandboxPolicy/ExecutionLease 无类型**(压 32→24):destructive deny list 0、secret-path deny 0(ADR-0011 §12.6 记为 ACCEPTED RISK:凭据文件可被读并送云模型);`resolve_write_target` 全 repo 唯一调用点在 propose 时(runtime/__init__.py:257),dispatch 不复查——无 defense-in-depth。
- risk ladder 与 §14.4 漂移:spec 把 file write 放 L2、L3 留给 push/delete/install/kill——write_file 自报 L3,而 §14.4 真正的 L3 类无任何工具。
- §15:12-state agent-run 机器 0 实现;§15.3 上下文包 2/11 字段(worker 看到的全部指令流 = `_JARVIS_AGENTS_MD` 的 submit_report 契约);waiting_for_input / blocked 协议 0 hits;submit_report schema 缺 blocked 扩展(有 status 枚举、无表达 why 的通道);`evidence_submitted` 仍被 `_worker_report_extras` 丢弃。
- 3/6 CallerPrincipal 是空集(WORKER_AGENT/BACKGROUND_SUBSCRIBER/SYSTEM_MAINTENANCE);`cancelled` 态无 emitter 生产不可达;write_file 从未在 live run 写出字节(C1 红,handler 正确拒绝 8/8——LLM 对不存在的文件 8/8 选 `append`)。
- 保住的强项:dispatch gauntlet(82)、result_semantics 接口(85,五值表查无 tool-name if/else)、SSRF guard(canonical reparse + 全地址检查 + redirect 复查)、8-态 lifecycle(76)。

### L5 surface(30)

- **daemon 侧整张路由表 inert**(层头条压 42→30):`inherent_loop.py:439` 传 `available_surfaces=frozenset()`,`cli_render.py:357` 全部 continue——常驻态零 say/osascript/banner/stdout;三份 8 月 burn 全走 `run_turn()`,从未 boot `serve_inherent`,所以 WS panel、TTSPipeline、WakeListener、`/inherent/asr-submit`、repo observer 全部零 live 证据。
- 语音输入从未在麦克风前烧过:ADR-0005 §11 (a)(b)(c) 无 committed 记录;AsrNormalizer 以空表构造(`corrections=[], aliases={}`,inherent_loop.py:729)——644 行归一化代码 runtime 什么都不归一。
- daemon TTS env-gated 且从未 burn(inherent_loop.py:764-769 无 key 即整体跳过,macos_say fallback 不独立接线);已烧的 `say` 是 cli_render 全文 one-shot,不触 §3.6.6(无逐句流、无 gate mode、无 ducking)。
- Panel = 4 envelope 文本卡:§18.3 十项内容(task 视图/agent 状态/evidence/diff/确认卡/checklist/mode pill)全无,`ViewModel` 0 hits——而 §18.12 rule 2 把它定为**默认工作 surface**。
- 5/9 attention channel 死 codomain(badge_card / soft_suggest / interrupt_now / delegate_agent / suppress 无 producer);§3.6.4 五项选路输入(可用性/新鲜度/近期活动/fallback 链/焦点)零实现;§18.10 三维选路与 §18.11 Mode×Surface 无任何代码。
- PresentationIntent 0 hits;surface.failed + fallback 链 0(仅两条 docstring 自认 defer);coalescing / rate limit 0(deliver_voice/banner 无条件每次都响,违 §18.4 与 §18.12 rule 4);multimodal staging = HTTP 501;Dashboard/Cockpit 0;surface.dismissed/clarified 注册了零 emitter。
- 记分口径修正:Agent Handoff 的 70 分是 L4 代码(codex_action.py,"no jarvis/surface/ file participates"),单行虚抬 L5 约 3.5 pt——已剔除。

### L6 deployment(36)

- **sleep/wake 头**(压 58→34):生产 `_IOKitPowerObserver` 从未 fire——生产 log 有史以来 0 条 mac.sleeping/mac.awake/worker.*_by_sleep;K7/K8 全靠 StubPowerObserver seam。三条 §3.7.8 mandate 未满足:`_before_sleep` 不做 WAL checkpoint(journal_mode=wal + synchronous=NORMAL,断电型 sleep 可丢已提交事件);`reconcile_after_wake` 过滤 `run_id is not None`,dispatched-未-run 的 orphan 被 wake 跳过、等 700s sweep;`mac.sleeping` reason 硬编码。另有 ADR-0009 D3 漂移:`_slept_for_ms`(sleep_wake.py:1091)扫 log 找上一条 mac.sleeping 而非读 kern.sleeptime——sleep hook 丢失时(spec 明说要预期的场景)报 0。
- **artifact store**(压 65→42):L6 贡献 = 两个 3 行 mkdir helper;真正的 staging(sha256/字节数/写盘)在 L3;`pending_writes/` 在生产 root **不存在**(只在 tmpdir burn 过);size_bytes/mime_type/retention 全树 0——无保留梯子、无过期、无降级。
- **launchd**(压 80→68):residency proven-not-adopted——`~/Library/LaunchAgents` 无 com.allen.jarvis.plist、无 `jarvis serve` 进程、`daemon.lock` 存着死 pid 7691;M1/M2/M8 全靠 0a8ae23 commit body 自述 + 1.9K log,零自动化测试(tests/unit 被 5ef27d1 连带清空);respawn 对 event log 不可见(46 类型无任何 daemon 生命周期事件)。
- 两列 provenance 结构性不可读:`_row_to_event`(event_log.py:1186)按 8 列 destructure,`_SELECT_ALL_ORDERED_SQL`(:1212)不 SELECT actor/ingestion_node,Event dataclass 无此字段——schema v1 交付的两列对全部消费者 write-only;`ingestion_node` 且与 §3.7.10 的 `origin_domain` 名字不符。
- federation 半边全 0(独立复核):46 注册类型无 cross_domain.* / scheduler.* / domain_availability.* / domain_projection.*;jarvis/ + config/ 无 mqtt 字符串;RPi node 无代码。standing defer(ADR-0009 §12),但它就是 §3.7 的正文。
- 全绿孤例:supervisor sweep(82)——生产 log rows 916-934 逐行复核成立,唯一 "live_verified" 完全够格的 L6 组件;扣分仅因 orphan 是手工种的、全 log 只有 3 条 timeout_assumed。

### runtime 组合根(58)

- **forward mode 有具体 bug 且是默认生产路径**(压 52→35):`_collect_response`(cli/__init__.py:230-271)只在 `open` envelope 检查 turn_id,之后的 `append`/`done` 不检查——daemon 是多 producer(wake listener + /inherent/submit),语音 turn 与转发 CLI turn 交错会串文或截断;而 cli/__init__.py:897 在锁被持有**或** LaunchAgent 已安装时默认走 forward。已 live-verified 的 fork-detach 分支在装机后反而是 fallback。
- `final_attention_channel` 默认字面量 `"voice_notify"`(runtime/__init__.py:1240)——L3 没设通道时 runtime 自己开口说话,安静优先(§3.2.5)spec 里最不安全的默认。
- serve_inherent(压 62→50):~190 行有序 teardown + 6 并发任务,方差最高、验证最少;`--live-daemon` 套件确认不存在(全 repo 0 hits),唯一真实世界数据点是 M1 respawn-loop **生产故障**(0a8ae23 修复)。
- §3.8 盘点缺口:inv 5(execution 不能自证——本库最承重的不变量)被跳过未评;inv 1 有四个静默持久效应(clean stash pop 无事件 runtime:1470、非冲突 StashError 吞掉 :1460-1469、`_child_run` 失败进 /dev/null 无 turn.failed cli:591-622、observer 事件改 belief 不触发任何 turn)。
- 3/6 turn trigger 无 producer(scheduler.fired / sensor.alert / cross_domain.response.received,grep 0);waiter 生产中从不真正阻塞(spawn_worker inline 先写行)——多 trigger loop 结构性同步,`_LIVE_ACTIONS_BY_TURN` 等进程级可变字典从未受两并发 turn 的真实压力。
- snapshot 无 high-water mark:每 turn 最多 4 次全量 fold + finally 里每 turn 一次全 log walk(:1426);实测 1203 行 6.5ms/fold——今天是噪音,1e5 行时 ~2s/turn。**flag 不 fix**(deflator 认可此判断)。
- 保住的强项:stash 泄漏按构造关闭(b770a97,finally + 三终态扫描,复核成立);boot 不变量的 loud/inert 姿态逐点有理有据;composition root 同步半边的 live_verified 合法。

---

## 4. Burn 覆盖三色名单

### 绿(live green)

| 行 | 代码点 | 记录 |
|---|---|---|
| A1-A8, B1-B4, C1-C5, D1-D5, E1-E4, G1-G5, I1-I2(flagship 正路) | dcac425 | 2026-08-25 全套 burn,log 2ccb5c2 |
| F1-F6, I3(verify-fail 负路) | dcac425 | 同上 |
| J1, J2, J4-J9, J11(real-Codex 生命周期) | dcac425 | 同上 |
| J13 两个 live 可达 dirty-tree 形状 | dcac425 | 同上 |
| K3-K8(detached CLI / limitation 语音 / delivered_via / sleep-orphan / wake 幂等) | dcac425 | 同上 |
| L1-L3 + L4 不变量(空 diff / no-verify / verified / 强制 limitation 语言) | dcac425 | 同上(L4 行寄生在 test_l1 名下,traceability 债) |
| T1-T4, T6, T7, E1, E2(ADR-0011 工具面) | 0e5f29a | docs/live-burn-2026-08-26-adr0011.md;**one-shot 脚本,未提交,无 pytest** |
| C2-C6(ADR-0012 确认流) | ded19db | docs/live-burn-2026-08-26-adr0012.md;**同上** |

### 红 / pending

- **L5 — RED(committed fail)**:reviewer 在 b8ba97c full-diff 修复后系统性给 `supports`(两次独立复现),`refutes` 断言失败;open decision(retune goal vs re-scope part (g));它同时是全库唯一 live refutes 覆盖。
- **C1 — RED**:确认链在 log 上完整正确(requested→accepted→gate pass with lease→dispatched→result_observed),然后 write_file 因 LLM 8/8 选 `mode='append'` 对不存在文件而正确拒绝——工具面缺存在性信号(§10.6 M,OPEN)。目标文件从未被创建。
- **Finding N — OPEN**:机器 ask 只 arm ~50%(20 次里 10 次 prose 抢答,`tools=[]` 无槽无 grammar);绿行靠每行最多 4 次 retry。唯一统计记录 `burn_adr0012_v2_out.json` 不在盘上。
- **T5(screen_look)— PENDING**:macOS TCC 弹窗需 Allen;68/68 hermetic 行明确不替代。
- **C1 spoken 端到端 — PENDING**:需 Allen 在麦克风前;且被 C1 红行前置阻塞。
- **ADR-0005 §11 (a)(b)(c) 音频 smoke — PENDING Allen**:自 2026-05-30 无变化。
- **M1/M2/M3(ADR-0009 residency)— PENDING**:Allen-supervised by design;M1/M2 有 0a8ae23 commit-body 自述(launchd 起来过、kill -9 respawn 0.3s),M3(真 pmset sleep)从未作为行跑过。
- ADR-0011 DoD "registry 42" — 按写法不可满足(entity.resolved 已存在),errata §12.4 N 记录,registry 停在 41→46。

### Deferred by design(不算红)

J12(P-0010 + never-patch-prompts)· J13 真 merge-conflict 形状(sandbox 拒 .git 写,有 pin)· M3 自动化(机器会睡着,logistics)· ADR-0011 §11 defer 表(reminder/scheduler、Hue→RPi、idle_proactivity 等)· ADR-0012 §10.7 携带债(pending_writes 无清理、scratch bookmark 缺、None-branch 工具表过期、两个 thin surface.* 占位)。

### Not covered(零运行覆盖 / 记录缺口)

- **J3, J10, K1, K2**:skip message 指向 `~/Projects/jarvis-deprecated-tests/`(5ef27d1 移出 repo,自称不维护、非 git repo、无 gate 执行)——四个不变量零运行覆盖。J13 conflict 分支与 J12 后置守卫的 proof 同样在库外。
- **M4-M8**:行 specified 从未建成——无 `--live-daemon` flag、无 M 命名测试、无 ADR-0009 burn log;§8 DoD item 5 unmet 且无人追踪。(实现本身已在 main。)
- **15 个 Phase-2 行(T1-T7/E1/E2/C1-C6)无 pytest 表示、不可重跑**:burn/verify 脚本未提交;复验 = 从 prose 重写 harness。
- **open findings 无 bug ID**:docs/live-run-bugs.md 停在 B-0014,还断言 "No code bug remains open";finding N 这种 live 可靠性缺陷只活在 ADR prose 里。
- ADR-0002 §8 L5 行文字(line 1882 仍要求 refutes)未在 burn 反证后修订(ADR-0002 无 errata 节)。
- A1 事件数窗口 [22,60](test_flagship.py:83-84)vs ADR-0002 Phase-2 收紧值 [34,38]——唯一能抓 0011/0012 事件数漂移的行松了 5 倍。
- docs/progress.md 停在 "ADR-0009 Step 2"——三份 burn log 与两个 ADR errata 节是仅存的现行记录。
- T8 不存在:ADR-0011 §8 就是 T1-T7 + E1/E2 九行,任何 "T1-T8" 名册是误抄。

---

## 5. 证据新鲜度与最小恢复动作

- 最后一次全套 pytest live burn = **2026-08-25 @ dcac425**(43/11/1,25:08 wall)。其后 **20 commits、jarvis/ 下 +6090/−403**:tools.py +2741(LLM 菜单 6→14,含 write_file——A-I/J/K/L 每行最大的行为变量整体换血)、gates.py +504(entity arm + lease v2)、packet.py +90(block 8 进每个 prompt)、decision/__init__.py +1276。**HEAD 上没有任何 pytest live 行跑过。**
- T 行(0e5f29a)落后整整一个 ADR-0012 栈;C 行(ded19db = HEAD~3)对 HEAD 代码 fresh(其后两 commit 均 docs-only)。
- Tier-1 hermetic @ HEAD 绿(109 passed / 56 skipped, 4.4s)——但 56 个 skip 恰是全部 live 行,Tier-1 绿对本审计的任何行不构成证据。
- **最小动作**:在 a0f6264 原样重跑 08-25 invocation(`uv run pytest tests/scenarios tests/integration --live-llm --live-codex -v`,~25 min wall),对比 43/11/1——这是知道菜单 6→14 是否动摇 flagship/J/K/L 行的唯一途径,live 成本按既定方针不设限。

---

*来源:workflow `wf_da9a6587-a20`(2026-08-26),15 agents。汇总 JSON 与逐 agent 原始返回见 session `ec7a7041` 的 tasks/wiwzv095m.output 与 subagents/workflows/wf_da9a6587-a20/journal.jsonl。*
