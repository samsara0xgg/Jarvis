# 语音交互体验审计与改造：交接记录

整理日期：2026-09-28，2026-09-29 按五次 Mac 实测更新。分支 `claude/peaceful-pasteur-wv870t`（基于 main `ae1e10f`），
main 三次合并进来：`c6b3e6c`（`c909f5a`）、`a805b44` B01 Long Exposure（`cb675ab`）、`daa0536`（`085a6fc`），都不改写已推送的历史。落进 main 时仍须线性：
按 `docs/git-guide.md` §3 把本分支自己的提交变基到 main 上再快进，`Companion.tsx` 的冲突照 `c909f5a` 解。
性质：跨 session 的工作底稿，合并时删除；不是 ADR，也不是合同。

## 1. 任务

Allen 的要求：严格审计 Jarvis 的语音交互系统（全双工对话 + 语音过程中的交互，
如弹卡片、展示信息；先重逻辑、不做 UI 精修），参考 GPT-Live 取其精华，
给出让所有人都有最自然、舒服的人机语音对话的设计与实现，作为作品集级交付。

前端只看星核 companion：`electron/companion.ts` → `index.html?companion` →
`src/Companion.tsx`（`main.tsx:406`），及 `CompanionBall.tsx`、`starCore.ts`、
`Notices.tsx`、`ActionCard.tsx`、`soundKit.ts`、`runtime.ts`、`model.ts`。
`main.tsx` 里的旧胶囊 `App`（VoicePresence/LivePresence/LiveTranscript…）已淘汰，不动。

**优先级（Allen，2026-09-28）：GPT-Live 的任务全部延后，先把他自己的实时语音（本地链：
单一音频入口 → Silero VAD → SenseVoice → L3 → 口语版 → MiniMax 流式 TTS → 星核）做好。**
§3 按此排序；GPT-Live 条目集中在 §3.3，不要先做。

## 2. 已完成（均已推送）

| 提交 | 内容 | 证据 |
|---|---|---|
| `d477088` | 端点：短音（<`min_voiced_s`）后停顿锁存了 VAD 的 `empty()`，下一句说到约 0.2 s 被切成两句；短音后无话则话语在飞 30 s，按 ADR 0074 扣住所有回答。现：`empty()` 在重新开口时清除；只在静音帧结束；短音多等 `short_sound_grace_ms`(400) 后以 `short_pause` 结束交给 ASR 判定 | `tests/integration/test_voice_short_sounds.py` 7 例，父提交上 6 例失败 |
| `e5d4ca6` | GPT-Live：被取代的查询在取代时即记 `withheld`（原先会话先关则下次会话会念出旧答案）；`_speech_cut` 认英文句号（原先 300 字符以上英文答案永不朗读）；空请求窗口改为 commentary（原先 thinking 不触发说话，Live 说完"我查一下"就沉默）；扬声器打不开则拒绝开会话；persona 补回官方模板两行（纠正要委托、依赖后台的回答先委托） | `test_live_delivery.py` 新增 4 例父提交失败 + 4 个切句钉子。persona 改动仍欠 Mac 实测 |
| `efd3d5a` | 每个展示过的回答都以 `voice spoken` 收尾：TTS watcher 丢弃的静默回合（badge_card/queue_review/gpt_live）发 `suppressed`；无语音管线的开机发 `no_voice`；排队后被丢的回答发 `dropped`。原先星核会一直张嘴停在"说话中" | `test_every_turn_ends_its_speaking_face.py` 3 例，父提交全失败 |
| `e4748e6` | （P0 #1）完整播完的回答不再被告诉模型"被打断"：`HeardPrefix.complete`（终态为 `playback_completed` 且覆盖全部已准备分段）；其余情况去空白后再比较 | `test_previous_answer_line.py` 英/中两例父提交失败，另一例钉住真打断仍引用已听到的句子 |
| `69f3fae` | （P1 #9/#10）首个样本前被取消（`submitted_samples` 0、quality `unknown`）记为空前缀，下一轮说"一个字没说出口"；截断行越过没得到回答的轮次往前找上一个回答，后来的回答终止回溯。spec §10 状态块一句随改 | 同文件新增 3 例，2 例父提交失败 |
| `6853af3` | （P0 #3 后端）`/inherent/cancel-response` 接 `turn_id`：该轮所有未结束的 run 按 generation 取消（`no_open_run` 表示已无）；命名的 `foreground_output` 停止对尚未出声的回答（排队、被 ADR 0053 停住、缓冲中）直接丢弃并发 `spoken/dropped`，原先答 `stale` 后照播 | `test_stop_reaches_the_answer_shown.py` 2 例，各自在无对应修复时失败 |
| `bb4f645` | （P0 #2–#5）星核的脸由回合推出而非共用 `phase`：在飞的话 → 在听；`turnId` 的回答从 `open` 到 `spoken` → 在说/在想；本界面发起、仍在等的回合 → 在想（打字、卡片也算）。戳一下：在说停该回答，在想按 `turn_id` 停该轮。Dashboard 尾行与气泡同用 held 文本；被取消的在显示回答一律清掉；等待中回合的失败不论 phase 都显示；`spoken` 结束静默回合的等待；断线结束当前回答；`open` 先于提交回执到达时不再等 | `verify-companion-live.mjs` 新增 6 项，父提交全部失败；合并 main 前 120/120 通过（headless Chromium + 假 daemon）。合并时与 main 的 `f2c1f95` 冲突于 `Companion.tsx`（main 把 Dashboard 移进 `dashboardContent`，`busy` 随之改用 `voice === 'thinking'`） |
| `fe24c3a` | （P1 #7）软打断，按 §4 设计实现：会话模式下她说话时 Allen 开口先降到 0.2；浊音满 0.4 s 才停（停下后才恢复音量）；更短的声音静 350 ms 即结束交给最终识别：没字/附和恢复且不成回合，停止请求或只叫唤醒词停且不成回合，其余停并成回合；已停的若是停止词/附和也不成回合；静音 × 让步增益相乘，让步永不解除静音；`barge_in_confirm_voiced_s: 0` = ADR 0041 原样。spec §3.6.5 随改；决定写成 `docs/plans/soft-barge-in-proposal.md`（Proposed，待 Allen 接受后编号并 supersede 0041） | `test_soft_barge_in.py` 12 例（真会话 + 出厂 VAD + 真 pipeline + 事件日志），含晚到的停止不提前恢复音量。**欠 Mac 实测**（reSpeaker、外放各一次；0.4/0.2/350 未校准） |

| `629e408` | （Allen 用 Codex 做）明确要求数数、朗读、逐字复述、详细讲或指定长度时，口语版不再压成 60 字/一两句，按要求说全（ADR 0082 取代 0045）。**编号冲突**：Allen stash 里未提交的 agents workbench ADR 草稿也叫 0082，落 main 时改一个 | Codex 的 4 个真模型用例（中英 1–50、逐字朗读、短默认）。欠 Mac 实测 |
| `9a1c919` | （Allen 用 Codex 做）MiniMax `subtitle_type: word_streaming` 的逐字时间戳落进 `surface.playback_alignment`，听到的前缀可以停在字上而不是整段（ADR 0083）；让步/打断前已播的字保留 | `test_word_playback_cursor.py`；MiniMax 真合成 + 播放器重放 + LLM：停在 15，答"数到 15"。欠外放实测 |
| `92f072a` | 按第一次实测：她声音上把「停」听成的 ting/ding 单音（「停立」「顶」）、「等一下」、wait 算停止；一个字/一个词又不是回答字的（「五」"And."）算 `unclear`，恢复音量、不成回合（单独的对/是/好/yes 仍成回合，ADR 0062）；`barge_in_confirm_voiced_s` 0.4 → 0.8 | `test_soft_barge_in.py` 新增 7 例，父提交全失败；录音重放：那声「嗯」浊音 0.61 s，「对对对」0.64 s |
| `2229550` | 播放器只在出声时消费增益命令：打断停下后恢复 1.0 的命令留到下一个回答才以 30 ms 斜坡生效，下一个回答开头在 0.2 上起步、整段被记成 attenuated，于是"没听全"（实测里打断后的完整回答 heard_text 全空）；静音后下一个回答首块漏音同理。现：空闲回调立即落地 | 同文件 2 例，父提交失败 |
| `4eb10a0` | "pause" 算英文停止词（单个英文词现在算 unclear） | 同文件 1 例 |
| `65d0502` | 语义端点离线校准（见 §3.1 第 3 条），`candidate_ms` 320 → 400、`max_hold_ms` 900 → 800；开关仍关（tier B，需先实测） | `test_endpointing_partial_asr.py` 1 例 + 表 1 行 |
| `656a0b8` | （§3.1 第 9 条）整句只是「什么？」「啊？」「再说一遍」「没听清」、what? / pardon / say that again 时，L3 在 Tier 0 前把最近一个回答的语音部分原样再说一遍，不调模型（spec §17） | `test_repeat_request.py` 10 种说法父提交全失败，4 个近似句仍走模型 |
| `ed1792d` | （§3.1 第 2 条 i）TTS 预热：ADR 0053 的 hold（Allen 开口）时连好一个 MiniMax 会话（connect + task_start），回答的首个 endpoint 直接 bind；超过 60 s 的关掉重连（MiniMax 120 s 无事件断开），正在连的留给下一个回答，睡眠/关闭时关掉。TLS 到 api-uw 实测 0.11–0.15 s | `test_tts_prewarm.py` 4 例父提交全失败。第二次实测：open 0 ms，首包 0.15–0.36 s |
| `a8234a6` | 按第二次实测：「OK可以了」（够了）算停止，前面可带 ok/okay；单独的「可以」仍是卡片的"是" | `test_soft_barge_in.py` 1 例父提交失败 |
| `1a0b026` | 按第二次实测：她说话时 7 次 pause 都被识别成别的单个英文词（5–6 字符，Allen 说是 Pulse），合成的 pause 识别成 "Cause."；两者算停止 | 同文件 2 例父提交失败 |
| `609d253` | （Allen 选 A）浊音满 0.8 s 不再直接停掉，而是停在原处：播放器不再消费该 generation、位置保留（`pause_generation`），最终识别是附和、没字或一个无意义的字就从原处接着说（128 样本淡入），停止请求和其他话才停掉，识别出错也停掉。spec §3.6.5、`config/jarvis.yaml` 注释、软打断提案随改 | 同文件：长「嗯」、长咳嗽各 1 例，播放器保位与淡入、下一个回答不受影响各 1 例，父提交全失败 |
| `609d6f9` | 按第三次实测：她说话时单独一个英文词（附和声、卡片回答词、问句除外）算停止，因为 pause 每次被听成不同的英文词（2–6 字符）；"And." 算附和；「可以啦」「够啦」这类「啦」尾算停止；复述路由的「什么」「啥」不再要求问号（识别给的是「什么。」）。spec §3.6.5 与 §17、`config/jarvis.yaml` 注释、软打断提案随改 | `test_soft_barge_in.py` 3 例、`test_repeat_request.py` 1 例父提交失败 |
| `d58edd6` | 按第四次实测：日文、韩文的嗯（うん、う、ん、응、음 这类）算附和。拖长的「嗯——」被 `tts` VAD 断成三段、识别成「うん」「うん」「う」，第一段当成一句话停掉了她 | `test_soft_barge_in.py` 2 例父提交失败 |
| `0a4bb88` | 按第四次实测：打断时她的声音从一个字中间截断，截断处只有合成的短淡出（ADR-0006 D11，48 kHz 下约 2.7 ms），Allen 听到爆音。软打断的停掉和停在原处现在先用 gain 在 20 ms 内把真实波形淡到无声，再截断或停住；停住后恢复按 yield 的 30 ms 淡入。停止按钮和其他截断不变 | `test_wave2_streaming_media.py` 2 例，去掉淡出时都失败 |
| `35a9355` | §3.1 第 6 项：停止按钮也先在 20 ms 内淡出再截断（之前只有打断这样）；打断的淡出由发起线程直接下发，媒体线程被 SQLite 卡住时她也马上静下来，没有东西可停时增益还回去；回答音频断流后接上时，第一块从无声升回去（D12 尾部 ramp 反过来），不再直接跳回波形 | `test_wave2_streaming_media.py` 新增 3 例，去掉对应改动时各自失败 |

另两个提交：`8658592` 让星核验收脚本按 main 的新手势（点刘海）开 Dashboard；`2ae364a` 加
`tools/voice_live_report.py`，Mac 实测后一条命令打印每轮的话、回答、播放结局、听到多少和模型被告知的截断行
（见 §7）。

门禁（Linux 容器，合并 main 后的同一棵树）：lint-imports KEPT；ruff clean；mypy 仅 `jarvis/deployment/sleep_wake.py:486`
unreachable（Linux 平台分支，main 同样）；全量 hermetic 1214/1217，3 个失败在 main 同样失败
（`/usr/bin/security`、`test_timesink_activity` 与 `test_worker_confirmation` 各一）；星核验收 91 项通过，
停在 L13 的通知卡（断言早于 `f2c1f95` 的通知卡重设计；纯 main 上这个脚本在 L8 就停）；main 自己的
`verify-companion.mjs` 在容器里第一次戳就超时，纯 main 同样；`uv audit` 访问 api.osv.dev 被代理拒绝，未跑。

## 3. 待做，按优先级

证据行号基于 `ae1e10f`～`efd3d5a`，之后的提交可能挪动行号，动手前重读。条目编号沿用四路只读审计
（输出/打断链 = 输出 F*，星核前端 = 前端 F*，GPT-Live = Live F*）。

### 3.1 本地实时语音（先做，按顺序）

1. **Mac 实测**：第一次（2026-09-28 19:16–19:20，17 轮，只做了第 1 轮前几步和报数）结果见 §7，暴露的问题已由
   `92f072a`、`2229550`、`4eb10a0` 修（0.4 → 0.8 来自录音重放）。第二次到第五次（2026-09-29 12:55–15:49）
   结果见 §7，暴露的问题已由 `a8234a6`、`1a0b026`、`609d253`、`609d6f9`、`d58edd6`、`0a4bb88` 修，第五次全部通过（Allen："可以很完美"）。
   长「嗯」声音一弱就会被 `tts` VAD 断成几段，多数不到 0.8 s 浊音，只小声；第五次有一段停在原处后接着讲。回声误触发这次没见到（那 7 次是 Allen 在说 pause）。
   新发现、未做：SenseVoice 自动语种在短音上会猜成粤语/韩语（实测「嗯」→「五」）。强制 zh 重解能纠正，但
   sherpa-onnx 只能整个识别器 `set_config` 换语种，换回自动后后续所有解码的语种判断都变了（实测），
   不能用；第二个 zh 识别器要多约 240 MB 内存，没做。
2. **首音延迟**（输出 F5，最大的自然度杠杆）。**目标（Allen 2026-09-29 定）**：① 闲聊从他说完到她开口中位约 1.5 s
   （当时约 4 s），第一句就是回答本身，不是"嗯""让我想想"；② 工具轮同样快地先说一句和内容有关的话，星核显示在做什么，
   结果出来接着说；③ 说了去做就一定去做，没做完不说做完了；④ 嘴上说的和屏幕上的完整内容来自同一次回答，出声前不再有
   第二次模型调用。做法（Allen："其他的都按你的想法"）：一次调用、先说后写、边写边说，去掉口语改写，每句念前过毫秒级规则；
   工具轮用模型自己的引导语（Responses API 的 `phase`，停在过程话就让它接着办，没说话的调用由第 4 项固定确认兜底）；
   边听边想；语义端点；最后按实测决定要不要开口更快的模型。D 不做。提案 `docs/plans/speak-as-written-proposal.md`（一份，
   两件事都取代 0082、用同一套机制）。
   **实现（2026-09-29，分支 `claude/peaceful-pasteur-wv870t`，开关 `realtime.response.spoken_streaming.enabled`
   关，tier B）**：Allen 说的一轮（`inherent_ptt`/`inherent_wake`/`speech`，且主模型在 api.openai.com）每次请求都经
   /v1/responses 流式发出；`final_answer` 文字过信封拆分和句子组装，按 `spoken-v1`（不拒任何句子）逐句出流，
   没有第二次调用；`commentary` 那句随 `action.proposed.lead_in` 走，commentary 开时在第一次为 Allen 办事的派发时
   代替固定确认说出（超 60 字、带标签或说成已完成的回落到固定句）；停在那句又没调用的回应补一次请求；一句都没出流
   （开头就有 markup）时整段按原文交付。回答写完前就出声还要开 `streaming_output.speak_from_segments`。
   证据：`tests/integration/test_spoken_streaming.py` 4 例、`test_lifecycle_commentary.py` 1 例、
   `test_typed_llm_stream.py` 3 例。欠：Mac 实测（三个开关一起开）；每次流新建 AsyncOpenAI 客户端和事件循环，
   多一次 TLS 握手（约 0.15 s）；接受后改 spec §3.6.5 "Spoken form" 与 §3.6.12 的 D6 注记，删旧 routine 路由。
   **第一次实测（2026-09-30 02:45Z，8 轮，PR #3 分支合并本分支、三个开关开，trace 在
   `~/.jarvis/logs/realtime-trace-2026-09-30-spoken.jsonl`，事件 id 19478–20388）**：闲聊从端点判定到出声 3.0 s
   （端点前静音 0.77 s 另算；请求前准备 0.3 s、首字 1.8 s、整句写完 0.4 s、合成 0.3 s）。8 轮 7 轮调工具
   （看时间 4、搜网 3），每多一次请求约 3 s，第一轮 6.1 s 就是先看了时间。离线重放这 8 句（每句三种：旧语音说明、
   新说明、切换前的 chat completions 无说明，24 次 0.10 美元，脚本和结果在
   `~/.jarvis-realtime-test/tool-replay-2026-09-30/`）：三种写法调工具的多少差不多（推荐电影三种都搜，打招呼三种都不调，天空只有一种搜），
   所以不是语音说明或新接口造成的，是主提示词「缺信息先用工具查」加上这次的问题本身偏查询；多出来的看时间和搜网是
   主提示词的事，要改得另议。说明里的工具名照样删了（点名只会招来调用）。模型搜网后在回答
   末尾写 OpenAI 引用标记（U+E200…U+E201），被念成 "cite turn0search0"，现念前去掉（整段交付的路径也去）。
   和 `tool_search` 一起写的那句等到第一个真正干活的派发才说（查日程晚 5 s），现在随它自己那次调用的派发说。
   每轮第一次请求只命中 system+工具的缓存（约 6.1k/45k），切换前就这样（chat completions 也是 5.9k），原因未查；
   `tool_search` 后工具表变了，下一次请求缓存全丢。复用连接实测每次请求省 0.15–0.2 s（新客户端 0.44–0.75 s，
   复用 0.33–0.38 s 取模型信息），要换成常驻事件循环加共享客户端，还没做。
   **轮次并发（Allen 实测提出）**：查邮件 30 多秒时他问了别的，新一轮和邮件轮同时跑；新一轮看到邮件问题没答又查
   一遍邮件，它的引导语打断了刚开始念的邮件回答，别的问题没答。Allen 要的是先回眼前的话，慢的结果回来后等她说完再
   说「对了，邮件查到了」。要改 ADR 0074/0044 的轮次规则，单独做。
   **实验（2026-09-29，50 次调用 0.18 美元，脚本和数据在 `~/.jarvis-realtime-test/speak-first-2026-09-29/`）**：
   真实提示 4.1 万 token（24 个工具、500 条历史），首字中位 gpt-6-luna 冷 1.47 s / 热 1.26 s，gpt-5.6-luna
   1.38 / 1.26 s；历史只留最近 12 条（6 千 token）1.04 / 0.86 s；第一句紧跟首字约 0.05 s。带引导语提示走 Responses
   API：8 个要办事的句子里 gpt-6-luna 说了不做 0 次（先说后调 4、直接调 3、按知识直接答 1），`phase` 全对；
   gpt-5.6-luna 说了不做 2 次且标成 `final_answer`。所以用 gpt-6-luna。生产上命中缓存的请求开口前也要约 1.6 s，
   只比不命中快约 0.2 s；每轮第一次请求只命中 system+tools 头部（约 5.9k），`tool_search` 之后那次请求缓存全丢。
   **以下为 2026-09-29 定目标前的记录。实测（2026-09-29）**：9-26 换 gpt-6-luna 后 69 轮语音，
   `utterance.received` → `surface.playback_started` p50 3.9 s、p75 6.3 s、p90 14 s（查东西的轮）；第一次实测的
   14 轮里主模型 1.5–6.2 s（中位 2.4）、口语版改写 0.8–1.7 s（中位 1.1）、TTS 6 个字的一段从激活到合成完
   0.48 s。主模型慢在先写完整段书面答案（190–550 字）。已问 Allen 三选一（2026-09-29）：A 先说后写（语音轮
   模型先写口语段、边写边说，典型早约 2 s，要新 ADR 推翻 0040 的否决理由）、B 只把改写改成边写边说 + TTS
   预热（省 0.5–0.8 s）、C 语音轮换小模型。之后细看代码（2026-09-29）改推荐为 **D：主回答仍用 gpt-6-luna，
   只把口语版改写换成小模型**（这一步 0.8–1.7 s，单一简单任务），因为 A、B 的"边写边说"都比预估大：
   现有 routine streaming 只在 `pre_route` 判为闲聊、不调工具时开（`config/tool_cues.yaml` 的工具线索很宽，
   "请/帮我/can you/search" 都算）；`stream_risk` 的正向句式规则会拒掉带我/你/请的句子，"已经/查到/search/check"
   等算 consequential，第一次拒绝就封住整轮；调完工具后的最终回答是批量请求，要流式得先建 ADR 0008 的
   第 9–10 步（语音/文档兄弟 run、流式工具调用）；run 的 policy 开 run 时就定死。Allen 还没选。trace 分解要在 launchd 环境里设
   `JARVIS_REALTIME_TRACE_JSONL`（`launchctl setenv` 后 kickstart）。原计划：等完整生成 + 常有第二次口语版 LLM 调用（中文 >60 字或有
   markup，`decision/__init__.py:2115-2199`）+ 每个回答新开 MiniMax WebSocket 与两次握手
   （`voice_media.py:2501-2512`，`voice_tts.py:2341-2387`），无预热（ADR-0006:295 要求过）；段间严格串行。
   修：(i) 预开备用会话（`MiniMaxTTSSession.open` 拆成 `connect()`/`bind()`，在 `hold_output(True)`
   即 Allen 开口时预热，用完补一个，空闲回收）；(ii) 口语版流式、首句先说；(iii) 段 N 出首音时发段 N+1。
   先确认 MiniMax 是否允许同一 socket 连续 task、流水 `task_continue`、空闲超时多久。估算语音到语音
   2.5–3.2 s（端点 0.83 s + ASR ~0.1 s + 生成 ~1.15 s + 口语版 0–0.72 s + TTS 首包 ~0.4 s），先用
   现有 trace（`tts_session_open_requested` → `tts_provider_first_pcm_received` →
   `audio_output_first_nonzero_callback`）实测分解再动手。
3. **端点 0.83 s**：语义端点（ADR-0006 D7 partial ASR）已离线校准（`65d0502`）：200 条录音
   （9-20..28，唤醒通道）按真 assembler + Silero + SenseVoice 重放，每个 partial 按实测解码时间落地。
   对比现在的 0.77 s 静音：320/900 中位早 448 ms、200 条里 29 条在话没说完时结束；400/800 中位早 352 ms、
   16 条；480/900 中位早 288 ms、11 条。提前结束的多是说完一个完整分句后停顿再接着说，ADR 0074 会合并作答；
   其中约 5 条是重放伪影（"The."、"Yeah."）。取 400/800：「嗯」之后停 1 s 再问仍是一句。开关仍关（tier B，
   canary `test_canary_realtime_adoption_tiers.py` 钉着）：第二次实测时在 `~/.jarvis/settings.yaml` 打开跑一轮，
   通过后改出厂值并把它挪进 tier A。重放脚本在该 session 的 scratchpad，没进仓库。
4. **工具慢时的口头回应**：原提案已并入 `docs/plans/speak-as-written-proposal.md`（2026-09-29，等 Allen 批，
   批后成为新 ADR 取代 0082）。9-26..29 的记录推翻了原设想：工具本身多在 0.5 s 内返回，慢在模型（最后一个结果到
   出声中位 3.9 s），"1 s 内有结果则不说"会让最慢的轮不说；纯按时间触发会让 3 s 时还没出声的 55/117 个
   闲聊轮也说。改为：第一次调用为 Allen 办事的工具（不含 tool_search、时钟、记忆、卡片）时说一句确认，
   不早于他说完 1.5 s、回答还没出声才说，其余生命周期行不说。已实现（`a5a867d`，本地提交，开关仍关）。
   2026-09-29 定目标后它降为兜底：主路是模型自己说的引导语（第 2 条），模型没说话就调工具时才由它开口。
5. **从未出声的回答仍被当作听完**（§2 `69f3fae` 遗留）：排队中被停、或被 ADR 0074 取代前已写完的回答
   没有任何 playback 事件，`spoken_heard` 为 None，下一轮当作听完。需 L5 为"未出声即丢弃"写一条持久
   事件，或 L3 把没有激活记录的语音回答当作未说出；先定语义。
6. **播放细节**（输出 F6/F9/F12/F8/F16）：空闲时增益命令不生效已修（`2229550`）；停止只有 2.7 ms 衰减、
   打断要等媒体线程、断流恢复无 ramp 已修（`0a4bb88`、`35a9355`）。剩下：部分 TTS 失败中途无声无提示。
7. **星核跟真实音频**（前端 F9，需 ADR）：现在首个文本块即"说话"，嘴是正弦波（`starCore.ts:591-593`），
   气泡整段一次出现。daemon 发 `voice speaking`（播放真正开始）、`heard {sequence, heard_text}`
   （checkpoint）、10–20 Hz 输出/输入电平（复用听写的 `_level`），星核的嘴、气泡按播放进度走；
   软打断让步时给一个"停下来听"的表情。
8. **星核其余**：无麦克风/能力状态，静音或设备丢失仍显示"Listening…"，`voice_capability` 无处理
   （前端 F7，`runtime.ts:57-72`）；有卡片时语音对话无字幕/表情（F8，`Companion.tsx` 卡片分支）；
   卡片不告诉 Allen 能用嘴答（F10：`answers_by_words` 已在 `projections.py:543-549`，卡片路由不带，
   加 `words_open` 与最近一次关卡原因）；卡片点击乐观移除（F11）；daemon 重启/断线后免唤醒模式静默结束
   且星核不重发（F12）；进入提示音与 `conversation:true` 同时发出、可能被自己的麦克风听到（F13）；
   error/empty 原因被丢，识别失败看起来像沉默（F14）。
9. **修复用语**：已做（`656a0b8`）。放在 L3 而不是 L5：L5 不能自己出声（spec §3.6.5）。
10. **AEC 参考不全**（输出 F10/F11，本地部分）：`say` 兜底以子进程播放，不在参考里，会话模式下可能自我打断
    （改为 `say -o` 渲染成 PCM 走播放器，或 `macos_say` 期间不做打断）；AEC 参考环满时丢最新样本，
    停顿后参考错位（计溢出、`clean()` 里重启）。
11. **作品集文档**（§5）：在上述主干完成或实测有结果后写，发布为 Artifact。
12. **唤醒词和命令一口气说出时 Tier 0 全落空**（夜间挂机线程 2026-09-29 17:03 实测发现，未改）：转写
    「嘿ja班javis斯,我要睡觉了，能让他继续跑吗？」。`tier0.match_tier0`（`tier0.py:248`）拿原始转写匹配
    `^` 锚定的规则，没有地方去掉开头的唤醒词；`voice_pipeline.py:253` 只拒整句只有唤醒词的（`is_wake_only`）。
    这一轮于是转去调模型。`_WAKE_ONLY_RE` 认不全这类转写（「班」「斯」），更稳的是按唤醒词检测的结束时间切掉前段。
13. **聚合输出设备上压低音量可能无效**（同上）：默认输出是聚合设备 "Multi-Output Device 2"，其上 osascript 的
    `get volume settings` 返回 missing value，`voice_ducking.py:102-117` 的 AppleScript 多半不起作用。夜间挂机
    线程正把静音改成走 CoreAudio、对聚合设备的每个子设备分别静音，ducking 等它落地后照做。

### 3.2 本地链 P2（记录在案）

输出 F7（每段新建重采样器）、F13（起点后才开始播放的回答无法被打断）、F14（`retire_generation` 异常
卡死 lane）、F15（单一入口模式下从不压低其他 app 的声音）；前端 F15–F19（上一句残留在待机条、300 ms
单击延迟、卡片关闭后键盘焦点、`append` 无 turn id、麦克风开关同一提示音）。

### 3.3 GPT-Live（Allen 2026-09-28：延后）

- 请求按到达墙钟冻结（Live F3，`voice_live.py` `_settle_request`；阶段 C 清单 #13）：600 ms 无新片段即冻结、
  2 s 上限；官方"不要从缺事件推断静默"。修：等有 `start_ms ≥ offset_ms` 的片段再 settle，settle 后重算
  窗口；空窗口保留委托。先用日志片段时间统计校准。
- 播放器不是 AEC 参考（Live F4，`voice_live.py:575-581` 无 `playback_tap`），笔记本外放时可能听到自己。
- 静默挂断无提示、开始无就绪提示、断线无重连（Live F5/F6/F13）。
- 长查询死寂（Live F7）：提交时一条 thinking 后 90 s 内什么都不发。
- 任何新委托取代所有进行中的（Live F9），需 Allen 定 D5。
- P2：Live F14–F23（关会话时 ACK 可能丢、`bytes_pending` 不冲、切句不提示"其余在屏幕上"、事件循环上的
  阻塞调用、`usage.updated` 缺字段抛 KeyError、扬声器静音时仍按 commentary 计费、usage 无人读、brief
  忽略 `history_since` 且可含 mail、100 ms 输入块）。
- `e5d4ca6` 的 persona 两行仍欠一次 Live 实测。

## 4. 软打断

已实现（`fe24c3a`），设计与取舍见 `docs/plans/soft-barge-in-proposal.md`，合同见 spec §3.6.5。
提案 Status 为 Proposed：Allen 实测接受后编号进 `docs/adr/` 并 supersede 0041。

## 5. 设计总纲（作品集文档的骨架，尚未写成）

1. 永不压着人说话，让话也要让得好看（软打断、附和不打断、说话时不显示将被丢的字）。
2. 每一段沉默都有含义且看得出来：在听 / 听到了 / 在想 / 在说，由 daemon 真值驱动，~100 ms 内反应；
   工具慢时 1.5–2 s 内给一句不重复的口头回应（ADR 0045 关掉的 commentary 改为按延迟触发）。
3. 少说多展示：嘴上 1–3 句，屏幕放全文；卡片出现时语音指向它，卡片显示"可以用嘴答"的窗口。
4. 语音、触摸、键盘等价；卡片可以用话改、用话撤（已有 withdraw_card）。
5. 延迟是设计材料：目标 P50 语音到语音 ≤1.2 s（闲聊）、工具回合 ≤0.8 s 出第一声；主要杠杆是
   语义端点（ADR-0006 D7 已建未校准）、流式口语版、TTS 预热。
6. 修复是一等公民："什么？/再说一遍"复述、"停"只停不答、纠正即 supersede。
7. 诚实的存在感：表情跟真实音频走（电平、播放开始/结束），字幕跟播放进度走。

最后产出：`docs/` 下的审计与设计页 + 发布为 Artifact（作品集），列出已交付与路线图。

## 6. 容器环境（Linux 云端 session）

- `uv sync` 失败：`uv.lock` 只解析 `sys_platform == 'darwin'`。做法：`uv venv` 已建（Python 3.12），
  从 `uv.lock` 抽出 `name==version`（同名取最高版），`VIRTUAL_ENV=.venv uv pip install -r pins.txt`，
  再 `uv pip install --no-deps -e .`。PortAudio 缺失无妨（sounddevice 懒加载）。
- 星核验收：`cd desktop/resonance && npm ci --ignore-scripts && npx vite build`，再
  `ln -sf /opt/pw-browsers/chromium /opt/google/chrome/chrome`（脚本用 `channel: 'chrome'`），
  `node scripts/verify-companion-live.mjs`（假 daemon，约 2 分钟；L8 上滑那一项偶发抖动）。
- `uv audit`：容器里的 uv 0.8.17 没有 `audit`；`uvx --from uv uv audit …` 能跑但 api.osv.dev
  被代理拒绝。
- 门禁写法见 `docs/git-guide.md` §1。全量 hermetic 约 150 s。3 个环境性失败见 §2。
- 提交规范：`.claude/skills/commit/SKILL.md`；不加 AI 署名（CLAUDE.md）。推送到本分支，
  不直接推 main。

## 7. Mac 实测（2026-09-28 起）

准备：主 checkout（`~/Projects/jarvis`，launchd 从这里跑）。Allen 的 main 上有未提交的 agents workbench
工作（`desktop/resonance/electron/agents/*`、`src/agents/workbench/`、一份未提交的 agents workbench ADR 草稿），会挡住切换，
必须连未跟踪文件一起收起：

```bash
cd ~/Projects/jarvis
git stash push -u -m "wip agents workbench"
git fetch origin && git switch --detach origin/claude/peaceful-pasteur-wv870t
launchctl kickstart -k gui/$(id -u)/com.allen.jarvis
launchctl kickstart -k gui/$(id -u)/com.allen.jarvis.resonance    # 星核自动重建
# 测完：
.venv/bin/python tools/voice_live_report.py --minutes 20          # 输出贴回
git switch main && git stash pop
launchctl kickstart -k gui/$(id -u)/com.allen.jarvis
launchctl kickstart -k gui/$(id -u)/com.allen.jarvis.resonance
```

两轮，按顺序一次做完：

1. 会话模式（戳她进入）：「嗯……」停约 1 秒再说一个要长回答的问题（短音不切句）；她说话时说一声「嗯」或「对对」、
   咳一声（降音量后继续）；说「停」（停下、不回答）；问「你刚才说到哪了」（截断行）；让她完整说完一段英文，
   再问下一句（不再说被打断）；她说长回答时插话「等一下，那个……」停约 1.5 秒再说完问题（0.4 s 停下、半句不单独回答、
   截断行越过半句）。全程安静时音量不应自己掉（回声误触发）。reSpeaker 与 Mac 外放各做一次。
2. 表情与停止：问要查资料的问题，在想时戳（之后不出声）；问问题后立刻咳一声（回答照常来，出声时是说话脸，
   戳停的是回答不是会话）；打字问一句（在想的脸）；复制三行字后打字「读一下剪贴板」（不出声的卡片，脸不卡在说话）；
   可选：断网打字（气泡说原因）。

GPT-Live 那一轮延后（§3.3）。

结果（第一次，2026-09-28 19:16–19:20，Allen 只做了第 1 轮前几步和报数；Codex 修复都在这之后）：

- 〔1〕短音不切句：通过（「嗯……」+长城问题是一句，只答一次）。
- 〔7〕短附和：8 次都没打断、不成回合，通过。长一点的「嗯」（浊音 0.61 s）和一次咳嗽超过 0.4 s 直接停了她，
  其一被识别成 "And." 成了新问题；「嗯」被识别成粤语「五」、两次「停」被识别成「停立」「顶」，都被当问题回答。
- 〔5〕停在哪：失败。整段只有一个分段，中途停下时没有已完成分段，旧代码告诉模型"一个字没说出口"，
  模型编了「数到 50」「说到长城各段用途不同」。
- 另：「从 1 到 50 报数」只说「一到五十」、「我让你从一数到50」只说「好，我从一数到五十」、「说详细点」
  「再讲 100 字」都只说一句（→ `629e408`）；延迟见 §3.1 第 2 条。
- 没测：〔4〕英文、黄河插话、第 2 轮全部。

结果（第二次，2026-09-29 12:55–12:58，`a6337eb`，reSpeaker XVF3800 进、Multi-Output Device 2 出，
`settings.yaml` 软件 AEC 关；10 轮、她说话时 14 次出声；trace 开着）：

- 通过：「停」「等一下」只停不答（开口后 0.5–0.7 s 停）；问数到几答「二十」（截断行 "…二十，二十"）；
  两声短「嗯」小声后接着数；「那太阳呢」（插在月亮回答中间）直接答太阳；「再说一遍」走复述路由，0.41 s 出声。
- 失败：拖长的「嗯——」浊音满 0.8 s 停了她，识别为附和后她不再说（→ `609d253`）。
- 失败：「OK可以了」成了回合，模型当成"接着说"（→ `a8234a6`）。
- 失败：她说话时 7 次 pause 都成了 unclear（单个英文词 5–6 字符，间隔约 2.2 s），她只小声（→ `1a0b026`）。
  这类没成回合的声音不留录音也不留文字，只有 trace 的 `asr_final` 字数和 `conversation_barge_in_judged`。
- 小问题：「刚才你说的第一句是什么」复述的是 `document_text` 的第一句，不是她说出口的 `voice_text`；
  进会话模式后的第一句「从一数到30」录音里只有「30」（1.63 s，浊音约 0.6 s），她答了「30。」。
- 延迟（`utterance_committed` → `audio_output_first_nonzero_callback`，9 个模型轮）：中位 3.2 s（第一次 3.4，
  9-26 起 69 轮 3.9）。主模型 1.9–2.8 s；5/9 轮有口语版改写 1.0–2.1 s；长城那轮 web_search 1.6 s + 详细答案
  10.1 s + 改写 4.2 s = 18.8 s。
- 没测：「什么？」、卡片单字回答。

结果（第三次，2026-09-29 15:06–15:08，`d61b125`，设备和设置同上；4 轮、她说话时 6 次出声；trace 开着。
哪声是什么按 Allen 的测试顺序和识别语种推断）：

- 失败：3 次 pause 成了 3 个不同的单个英文词（2、3、6 字符），判为 unclear，她只小声（→ `609d6f9`）。
- 失败：「可以啦」（识别语种 yue）成了回合，她停下后答「好，停在这里。」（→ `609d6f9`）。
- 失败：「什么。」（句号）没走复述路由，模型答了一句反问（→ `609d6f9`）。
- 按设计：「嗯」（浊音不到 0.8 s，判为附和）和咳嗽（判为 unclear）都只让她小声、接着说；停在原处没触发。
  她说完后的一声咳嗽成了空话，没成回合。

结果（第四次，2026-09-29 15:21–15:22，`4471afc`，设备和设置同上；7 轮、她说话时 5 次出声；trace 开着）：

- 通过：pause（识别成 6 字符的英文词）和「可以啦」都只停不答；短「嗯」判为附和，她小声后接着讲；
  「什么。」走复述路由，原样再说了上一句。
- 失败：拖长的「嗯——」被 `tts` VAD 断成三段，识别成日文「うん」「うん」「う」；第一段成了回合、停掉了她，
  第三段进来时她已停下，成了一轮，她用日文答了一句（→ `d58edd6`）。
- 失败：打断时她停下的那一瞬有爆音（Allen 确认是停住的那一下，不是变小声或随机）；那两分钟 CoreAudio 没有过载记录，
  下午前几次测试时反而有（→ `0a4bb88`）。
- 小问题：说「继续讲吧」时她把故事从头压缩着又讲了一遍（提示里有截断行），说「从上次停了的地方继续讲」才接上。

结果（第五次，2026-09-29 15:47–15:49，`b972eed`，设备和设置同上；6 轮、她说话时 9 次出声；trace 开着）：

- 通过：5 声「嗯」都判为附和，她接着讲；其中一声浊音满 0.8 s 停在原处（`conversation_barge_in_confirmed`），
  判为附和后从停住处接着讲。pause（6 字符英文词）和「可以啦」停下不答。Allen 没再听到爆音。
- 另一次停在原处是「继续讲刚才的故事」，判为回合，停掉她后接着讲故事。
- 小问题：一句「你多少。」（原话不确定）成了回合，和后面的「继续讲」合并后，模型答成了「你是想问我多少岁吗」。
