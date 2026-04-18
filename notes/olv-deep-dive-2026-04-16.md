# OLV Deep Dive：Jarvis 桌面端集成方案

**日期**：2026-04-16（含 Verification Addendum，源码级验证后重大修订）
**目标**：确定 Jarvis 与 Open-LLM-VTuber（OLV）的集成路径，并提炼可直接借鉴的工程细节
**前提**：后端完全保留 Jarvis，仅借鉴 OLV 的参数 / 代码 / 架构思路

> ⚠️ **重要**：本报告第一版基于 agent 代码扫描推断，有多处偏差。请优先阅读 **§7 Verification Addendum**（章节末尾），里面包含源码级验证修正，并引入了**路径 D** 这个第一版未考虑但可能最优的方案。

---

## 0. Executive Summary（已基于源码验证重写）

| 决策点 | 第一版结论 | **修订后结论（v2）** |
|---|---|---|
| 发现 | 以为要从零写 Electron + WebSocket adapter | **Jarvis 已有完整 Live2D web 前端 + FastAPI SSE 后端**（`ui/web/`）——7 个模型、pixi.js + Cubism 4、`handle_text(on_sentence=)` pattern 已工作 |
| 首选路径 | **路径 B**（OLV 前端 + Jarvis WebSocket adapter） | **路径 D**（把现有 `ui/web/` 用 Electron 壳包起来加 Pet Mode）+ **路径 B** 作为可选第二前端 |
| 路径 D 成本 | N/A | **~1 天**：Electron main.js 加 BrowserWindow 透明配置 + 点击穿透逻辑（~200 行） |
| 路径 B 成本 | ~400-500 行，半天 | 修订：**~250-350 行**，半天。现有 `ui/web/server.py` 的 SSE→Jarvis 桥接模式可直接复用 |
| Jarvis 后端动不动 | 不动 | 不动（两条路径都不动） |
| OLV 可抄的前 10 个参数 | 见 §2.6，估算首字延迟 -50~70% | **未经实测**，估算未变但需 Phase 2 实际 benchmark |
| OS 动作（开浏览器/截屏等） | 需自己加 skill | **`remote/` 模块已完整支持** 13 个动作（open_app/open_url/screenshot/type_text/media_control 等）|
| Jarvis 比 OLV 强 | 记忆层 | 记忆层 + **打断机制** + **完整 OS 远控** |
| Live2D 许可证 | 待查 | ✅ Live2D 官方示例模型，小规模商用 OK，已在 Jarvis 仓库 |

**修订后一句话结论**：走**路径 D**（用 Electron 包 Jarvis 现有 web 前端加 Pet Mode），~1 天完成核心能力；**路径 B** 作为日常使用 OLV dmg 的兼容层可选，独立 ~0.5 天。两条路径**都不触碰 Jarvis 后端**。

---

## 1. OLV WebSocket 协议拆解

### 1.1 总览
- **URL**：`ws://localhost:12393/client-ws`（纯 JSON，无 binary frame）
- **握手**：无认证。`accept()` 后 server 发 4 条初始化消息（`websocket_handler.py:156-176`）
- **关键依赖**：FastAPI + uvicorn + `websockets>=12.0`（Jarvis 的 requirements.txt:42 已有）

### 1.2 客户端 → 服务端消息（21 种，核心 5 种）

| type | 必须实现 | payload | 说明 |
|---|---|---|---|
| `mic-audio-data` | ✅ | `audio: List[float32]` | 流式推送录音块 |
| `mic-audio-end` | ✅ | — | 录音结束，触发 ASR→LLM→TTS |
| `text-input` | ✅ | `text: str`, `images?: List` | 文本输入 |
| `interrupt-signal` | ✅ | `text: str` (已听到的内容) | 用户打断 |
| `frontend-playback-complete` | ✅ | — | TTS 播放完毕（同步锁，必须等） |
| 其他 16 种 | nice-to-have | history/config/group/heartbeat | 后期补 |

### 1.3 服务端 → 客户端消息（16 种，核心 4 种）

| type | 必须 | payload | 说明 |
|---|---|---|---|
| `set-model-and-conf` | ✅ | `model_info`, `conf_name`, `conf_uid`, `client_uid` | 握手必发，否则前端黑屏 |
| `control` | ✅ | `text: "start-mic"\|"interrupt"\|"conversation-chain-start/end"` | 控制 |
| `audio` | ✅ | `audio: base64 str`, `volumes: List[float]` (每 20ms RMS), `display_text`, `actions.expressions` | **核心 TTS+嘴型 payload** |
| `user-input-transcription` | ✅ | `text: str` | ASR 结果回显 |

### 1.4 关键流程时序
```
Client                                   Server
  --- WS connect -------------------------→
                                    ←--- full-text / set-model-and-conf
                                    ←--- control "start-mic"
  --- mic-audio-data (多次) ---------------→
  --- mic-audio-end -----------------------→
                                    ←--- control "conversation-chain-start"
                                    ←--- user-input-transcription (ASR 回显)
                                    ←--- audio {audio, volumes, display_text, actions}
                                    ←--- audio ...
                                    ←--- backend-synth-complete
  --- frontend-playback-complete ----------→ (阻塞 server 直到收到)
                                    ←--- control "conversation-chain-end"
```

### 1.5 Adapter 工作量估算

**MVP（半天 4-6h，~400-500 行）**：
- [ ] FastAPI + uvicorn server 监听 12393
- [ ] 静态挂载 `live2d-models/`、`backgrounds/`、`cache/`（可直接软链到 OLV 资源目录）
- [ ] 握手 4 条消息（`model_info` 复制 OLV `model_dict.json` 即可）
- [ ] `text-input` → `jarvis_app.handle_text(text, on_sentence=cb)` → 每句发 `audio` payload
- [ ] TTS 文件落地：新增 `async_generate_audio(text) -> Path`（~50 行）
- [ ] 嘴型数据：抄 OLV `stream_audio.py:27-82` 的 pydub `make_chunks` + RMS（~30 行）
- [ ] `interrupt-signal` → `jarvis_app._cancel_current()`
- [ ] `frontend-playback-complete` 同步（必须等它才发下一条）
- [ ] `mic-audio-data` + `mic-audio-end` → `SpeechRecognizer.transcribe`

**完整对等（+2 天，~1200 行）**：
- history/config 对接、`actions.expressions` 情绪映射、`tool_call_status` 透传、`heartbeat-ack`

**不做**：群聊、`ai-speak-signal` 主动发话、`raw-audio-data` server-VAD、`/proxy-ws` 代理

### 1.6 核心坑（必须知道）

| # | 坑 | 影响 | 规避 |
|---|---|---|---|
| 1 | **同步锁 `frontend-playback-complete`** | server 必须等前端回这个 event 才发下一条 `audio`，否则 TTS 重叠 | 重写 TTSPipeline 的"本地播放"路径，改为"只发 base64 + 等 ack" |
| 2 | **Jarvis 是 sync+threaded，OLV 是 asyncio** | on_sentence 回调在线程池里，无法直接 `ws.send_text()` | 用 `asyncio.run_coroutine_threadsafe(ws.send_text(...), loop)` 推回 loop |
| 3 | **音频强制 WAV** | OLV 用 pydub 强转 WAV，edge-tts MP3 需先转 | 已有 ffmpeg 依赖，无问题 |
| 4 | **volumes 数组尺寸** | 10s 音频 = 500 个 float | JSON 够用，轻微带宽问题 |
| 5 | **握手 model_info 必须合法** | 否则前端黑屏 | 直接拷 OLV 的 `model_dict.json` |
| 6 | **TTS 顺序保证** | OLV `TTSTaskManager._sequence_counter` 保证并行合成顺序送达 | 抄这套 sequence 机制 |
| 7 | **无认证** | 暴露公网有风险 | Jarvis `remote/protocol.py:80` 有 token 模式可复用 |

**参考实现位置**（都在 `~/Projects/external/Open-LLM-VTuber/`）：
- `src/open_llm_vtuber/server.py:74-149`
- `src/open_llm_vtuber/routes.py:29`
- `src/open_llm_vtuber/websocket_handler.py:156-316`
- `src/open_llm_vtuber/conversations/conversation_utils.py:133-181`
- `src/open_llm_vtuber/utils/stream_audio.py:27-82`

---

## 2. 语音管线参数对比（Top 10 可直接抄）

### 2.1 一句话结论
**OLV 的 `faster_first_response=True` + `pre_buffer=640ms` + `vad prob+db AND 双阈值` 这三个改动，能把 Jarvis 的首字延迟砍 50-70% 并修复首字吞字**。其余 7 个改动是增量优化。

### 2.2 核心参数差异速查

| 类别 | OLV 策略 | Jarvis 现状 | 差距 |
|---|---|---|---|
| 首句切分 | 首个逗号即切，立刻发 TTS | 只在句号/问号/感叹号切 | **首字延迟 2-3s** → 可降到 0.7-1s |
| VAD 启动 | prob≥0.4 AND db≥60，3 帧（96ms） | prob≥0.5，250ms | 敏感度↑、误触↓ |
| 录音预缓冲 | deque maxlen=20 (640ms) | 无 | 修复首字吞字 |
| TTS 流式 | MiniMax `stream=True` + SSE 边传边解码 | `stream=False` | 首音延迟 ↓30-50% |
| TTS 预处理 | 5 个开关（忽略 emoji/括号/星号/尖括号/特殊字符） | 无 | 不再朗读 `*强调*` 和 `[tag]` |
| 断句算法 | pysbd 多语言，处理 Dr./i.e. 缩写 | 只处理数字小数点 | 混中英回复不再误切 |
| Hotwords | 支持 hotwords_file + hotwords_score=1.5 | 未接线 | Hue 别名、场景名识别率↑ |
| VAD 平滑 | 5 帧滑窗均值 | 无 | 空调/风扇环境抗抖 |
| 打断 memory 注入 | `[Interrupted by user]` + `heard_response+"..."` 覆盖 memory | 仅触发回调 | LLM 不重复被打断内容 |
| MiniMax vol | 1.0（OLV 默认） | **5.0**（Jarvis 可能爆音） | 降回 1.0 |

### 2.3 Top 10 可直接抄的改动（按收益排序）

| # | 改动 | OLV 源码位置 | Jarvis 改哪里 | 预期效果 |
|---|---|---|---|---|
| 1 | **首句逗号切分 + `faster_first_response`** | `sentence_divider.py:304, 492-505` + `conf.default.yaml:64` | `core/llm.py:591` `_SENTENCE_DELIMITERS` + 新增 `faster_first=True` 逻辑 | 首字延迟 -50~70% |
| 2 | **640ms 录音预缓冲** | `silero.py:103` `deque(maxlen=20)` | `core/audio_recorder.py` 加环形 buffer | 修复首字吞字 |
| 3 | **VAD prob+db 双阈值** | `silero.py:17-18, 138-140` | `config.yaml:39` 改 `vad_threshold: 0.4` + 新增 `vad_db_threshold: 60` | 敏感度↑ 误触↓ |
| 4 | **VAD 5 帧平滑** | `silero.py:21, 120-125` | VAD 调用处加 `deque(maxlen=5)` 均值 | 消除单帧抖动误触 |
| 5 | **打断 memory 注入** | `basic_memory_agent.py:195-223` | `core/interrupt_monitor.py` 补充：已播放文本 + `[Interrupted by user]` 写回 `conversation_history` | LLM 不重复被打断内容 |
| 6 | **TTS 预处理 5 开关** | `conf.default.yaml:469-476` + `utils/tts_preprocessor.py` | `core/tts.py` 合成前过滤 | 不再朗读 emoji/括号/`*强调*` |
| 7 | **MiniMax `stream=True` + SSE** | `minimax_tts.py:51, 72-81` | `core/tts.py:390` 改 stream 模式 | MiniMax 首音延迟 -30~50% |
| 8 | **pysbd 断句 + 缩写白名单** | `sentence_divider.py:31-46` | `core/llm.py:665-684` | 混中英不再在 Dr. 处误切 |
| 9 | **ASR hotwords** | `sherpa_onnx_asr.py:27-29` | `core/speech_recognizer.py:166-171` 传入 hotwords_file | Hue 别名识别率↑ |
| 10 | **MiniMax vol 1.0（不是 5.0）** | `minimax_tts.py:55` | `config.yaml` tts 段或 `core/tts.py:394` | 避免音量失真 |

### 2.4 次要改动
- OLV `interrupt_method` per-provider：Groq/OpenAI 用 `user` role，Claude 用 `system` role
- OLV `pronunciation_dict`：MiniMax 支持 `{"tone":["灯带/deng1 dai4"]}` 修正发音
- OLV `silero.py:79-188` IDLE/ACTIVE/INACTIVE 三态 + `<|PAUSE|>`/`<|RESUME|>` 标记

### 2.5 关键文件索引

**OLV**：
- VAD：`src/open_llm_vtuber/vad/silero.py:1-188`
- 句子切分：`src/open_llm_vtuber/utils/sentence_divider.py:304-515`
- 打断：`conversations/conversation_handler.py:112-143`、`agent/agents/basic_memory_agent.py:195-223`
- MiniMax：`tts/minimax_tts.py:48-86`
- TTS 预处理：`utils/tts_preprocessor.py`

**Jarvis**：
- TTSPipeline：`core/tts.py:668-807`
- 打断监控：`core/interrupt_monitor.py:45-335`
- ASR：`core/speech_recognizer.py:112-172`
- VAD 配置：`config.yaml:36-42, 579-584`
- 流式 LLM 分句：`core/llm.py:591, 657-684`

---

## 3. 架构层对比

### 3.1 Jarvis 比 OLV 强的地方（反向借鉴）

**记忆层**：Jarvis 远超 OLV。
- OLV：`basic_memory_agent` 就是 `self._memory: List[dict]` + JSON 文件，依赖 Letta 外包
- Jarvis：6 表 SQLite（memories/profile/episodes/digests/relations/observations）+ 三级 dedup + profile 重建 + episode 压缩 + observer 冷路径抽取（共 3963 行）

**打断机制**：Jarvis 更优
- OLV：前端 VAD 检测，必须用户说完整句才发 interrupt
- Jarvis：后端自建 streaming zipformer + Silero VAD 双层过滤，200ms 级快速打断

### 3.2 OLV 比 Jarvis 强的地方（Top 5 借鉴点）

| # | 模式 | OLV 位置 | 为什么值得学 | 改动量 |
|---|---|---|---|---|
| 1 | **AgentInterface + Factory** | `agent/agents/agent_interface.py:9-54` + `agent_factory.py:15-132` | Jarvis pipeline 锁死在 `jarvis.py` 单体，换引擎要动主循环。3 方法接口可让"当前实现""Letta""云端 agent"并列 | 思路借鉴 ~300 行 |
| 2 | **MCP 工具栈** | `mcpp/{server_registry, tool_adapter, tool_executor, mcp_client}.py` | 瞬间接入社区 MCP server（playwright/filesystem/time/search/obsidian）。`ToolRegistry` 现需自己写每个工具 | 基本整抄 ~600 行 + Jarvis 主循环改 async ~50 行 |
| 3 | **装饰器 pipeline** | `agent/transformers.py:12-217` | `sentence_divider → actions_extractor → display_processor → tts_filter` 每步独立装饰器，可测、可替换、可重排 | 思路借鉴 ~200 行 |
| 4 | **Actions 统一副通道** | `agent/output_types.py:7-16` | 未来要加 OLED 表情/LED 颜色/屏幕图像，用统一 `Actions(expressions, pictures, sounds)` 比散到 `event_bus` 干净 | ~80 行 |
| 5 | **LLM provider 池** | `conf.default.yaml:52-172` `agent_settings.llm_provider` 引用 `llm_configs` 池 | memory/observer 已经靠 override 绕过"单 provider"限制；正规做法就是 pool + reference | ~100 行 config + 30 处读点改 helper |

### 3.3 配置系统对比

- **OLV**：503 行，3 个顶层 section（system/character/live），层次化，LLM/ASR/TTS 是共享池
- **Jarvis**：584 行，22 个扁平 section，LLM 单例硬编码
- **差异**：Jarvis 加设备/skill 方便，但换 LLM provider 要改代码。OLV 反之。

---

## 4. OLV Electron Pet Mode 实现细节

### 4.1 关键技术点（如果走自建路径需要搞定的）

| 点 | OLV 实现方式 | 难度 |
|---|---|---|
| 透明无边框 | `transparent:true, frame:false, hasShadow:false` + Pet Mode 切 `backgroundColor:'#00000000'` | 低 |
| **鼠标点击穿透** | **默认全窗口穿透 + 组件 hover 上报解除穿透**（不是 per-pixel 系统级） | 中 |
| 拖拽 | 改 Cubism `modelMatrix` 平移，**不移动窗口**（窗口覆盖整个虚拟屏幕） | 中 |
| 滚轮缩放 | 缩放 Live2D 模型，不缩放窗口 | 低 |
| 眼神跟随 | 依赖 Cubism SDK 内建 `onDrag()` | 低（但窗口外鼠标不触发） |
| 右键菜单 | Electron `Menu.buildFromTemplate` + `screen.getCursorScreenPoint` | 低 |
| 模式切换 | Window ↔ Pet 共享同一个 BrowserWindow，**改属性不销毁** | 中 |
| Live2D 栈 | **Cubism 官方 Web SDK**（不是 pixi-live2d-display），v1.2 已换 | — |
| 嘴型同步 | Cubism 自带 `LAppWavFileHandler` + `audio.play()` | 低 |
| IPC | `contextBridge` 暴露 `window.api` + `window.electron`，main 只管窗口 | 低 |

### 4.2 点击穿透的巧妙设计（OLV 最独特的点）

**不是** per-pixel alpha hit-test（那种性能差、Mac 更难做），而是：
1. 默认全窗口 `setIgnoreMouseEvents(true)`
2. Renderer 里每个需要交互的组件 hover 时调 `window.api.updateComponentHover(id, true)`
3. Main 维护 `hoveringComponents: Set`，只要非空就 `setIgnoreMouseEvents(false)`
4. Live2D 命中靠 Cubism SDK 的 `model.anyhitTest()`
5. Windows 用 `setIgnoreMouseEvents(true, {forward: true})` 保持 mousemove 事件派发（macOS 不支持 forward）

### 4.3 Top 5 可 copy-paste 代码块（如果自建）
1. `main/window-manager.ts` createWindow + continueSetWindowModePet（多显示器虚拟矩形）约 40 行
2. `window.api.updateComponentHover` + hoveringComponents Set 穿透逻辑
3. `main/menu-manager.ts` 右键菜单 + 托盘菜单模板 约 180 行
4. `use-live2d-model.ts` handleMouseDown/Move/Up 拖拽（tap vs drag 判别）约 120 行
5. `use-live2d-resize.ts` 滚轮缩放 + DPR + 缓动 约 80 行

### 4.4 自建 vs 直接用
- **直接用 OLV 前端**：零成本拿到所有实现，绑定 Cubism Web SDK + Chakra UI + i18n，改 UI 要在别人代码里游走
- **自建借鉴**：Top 5 代码 copy 过来 1-2 天搭骨架，嘴型/表情/模型加载 Cubism 胶水 ~2000 行要么照搬要么换 pixi-live2d-display。综合 **1-2 周**达 OLV 功能对等

---

## 5. 最终建议

### 5.1 推荐：路径 B（Jarvis 后端 + OLV 前端）

**理由**：
1. **成本碾压**：MVP 4-6h vs 自建 1-2 周
2. **Jarvis 后端一字不动**：只新增 `ui/olv/` 模块做协议适配，核心 pipeline 不改，900 测试完全不影响
3. **前端许可证问题不存在**：私人使用不触发 `Open-LLM-VTuber License 1.0`
4. **符合用户约束**：后端完全是 Jarvis 自己的，OLV 只作为一个"可切换的显示客户端"
5. **未来退路**：如果哪天想自建前端，adapter 协议是标准 JSON WebSocket，前端随便换
6. **额外好处**：OLV 前端升级（Cubism 5 支持、bug 修复）Jarvis 自动受益

### 5.2 执行计划（2 周分阶段）

**Phase 1 — adapter MVP（半天）**
- [ ] `ui/olv/websocket_server.py`（~400 行，FastAPI + 5 个核心 message type）
- [ ] 从 OLV 拷 `model_dict.json` + `live2d-models/` 子集
- [ ] 验收：OLV Electron dmg 能连上 Jarvis，Pet Mode 里小月能听、能说、嘴型同步

**Phase 2 — 语音参数借鉴（1-2 天）**
- [ ] 改 Top 10 里的 1-4（首句逗号切分 / 预缓冲 / VAD 双阈值 / VAD 平滑）—— **这 4 个对延迟/准确度影响最大**
- [ ] 改 Top 10 里的 5-7（打断 memory 注入 / TTS 预处理 / MiniMax stream）
- [ ] 跑 `python -m pytest tests/ -q` 确保不回归
- [ ] 跑 `python system_tests/runner.py --mode cc --suite general` 验证端到端

**Phase 3 — adapter 对等（2 天）**
- [ ] 补 history/config/heartbeat/actions.expressions
- [ ] 处理所有坑（尤其 sync/async 桥接）

**Phase 4 — OS 动作 skills（按需）**
- [ ] 加浏览器打开、屏幕分享、Apple Script 控制等 skills 到 Jarvis ToolRegistry
- [ ] 这部分不依赖 OLV，是 Jarvis 独立工作

**Phase 5（可选）— 架构借鉴**
- [ ] AgentInterface 抽象（如果未来要加 Letta/云端 agent）
- [ ] MCP 工具栈（如果想瞬间接入社区工具生态）
- [ ] 装饰器 pipeline / Actions 副通道 / LLM provider 池

### 5.3 不做的事

- ❌ Fork Open-LLM-VTuber（用户明确要保留 Jarvis 后端）
- ❌ 自建 Electron 壳（ROI 太低，OLV 前端已经很好）
- ❌ 迁移 Jarvis 到 Letta 记忆（Jarvis 记忆层更强）
- ❌ 迁移 Jarvis 到 OLV 的 basic_memory_agent（架构倒退）

### 5.4 风险与退路

| 风险 | 缓解 |
|---|---|
| OLV 前端升级破坏协议 | 锁 v1.2.1，关自动更新 |
| 前端许可证商用限制 | 私人使用不触发；如果将来商用，再评估 |
| sync/async 桥接坑 | 已预判（见 §1.6 坑 #2），用 `run_coroutine_threadsafe` |
| frontend-playback-complete 同步锁 | 已预判，需重写 TTSPipeline 的本地播放路径 |
| 后期想加自己的 UI 元素 | 协议是标准 WebSocket，Phase 5 可以自己起一个简单 renderer 替换 |

---

## 6. 附录

### 6.1 OLV 本地路径索引
```
~/Projects/external/Open-LLM-VTuber/
├── src/open_llm_vtuber/
│   ├── server.py                           # FastAPI 主 server
│   ├── routes.py                           # WebSocket 路由
│   ├── websocket_handler.py                # 核心消息分发
│   ├── conversations/
│   │   ├── conversation_handler.py         # 打断处理
│   │   ├── conversation_utils.py           # 同步锁
│   │   └── single_conversation.py          # 单轮对话
│   ├── agent/
│   │   ├── agents/agent_interface.py       # Agent 抽象
│   │   ├── agent_factory.py
│   │   ├── agents/basic_memory_agent.py    # 主力 agent
│   │   ├── transformers.py                 # 装饰器链
│   │   └── output_types.py                 # Actions
│   ├── mcpp/                               # MCP 工具栈
│   ├── vad/silero.py                       # VAD 状态机
│   ├── asr/sherpa_onnx_asr.py             # ASR
│   ├── tts/minimax_tts.py                 # MiniMax TTS
│   ├── utils/
│   │   ├── sentence_divider.py             # pysbd 断句
│   │   ├── stream_audio.py                 # 音频 chunk + RMS
│   │   └── tts_preprocessor.py             # 5 开关预处理
│   └── live2d_model.py                     # 情绪提取
├── config_templates/conf.default.yaml      # 完整配置模板
├── mcp_servers.json                        # MCP 服务注册
├── model_dict.json                         # Live2D emotionMap
└── live2d-models/                          # 模型资源
```

### 6.2 Jarvis 改动文件清单（Phase 1-3）
```
ui/olv/
├── __init__.py
├── websocket_server.py           # 新增，~400-500 行
├── message_schemas.py            # 新增，~100 行
├── audio_encoder.py              # 新增，~80 行（RMS + base64）
└── model_info.json               # 从 OLV 复制

core/
├── llm.py                        # 改首句切分逻辑（Top 10 #1, #8）
├── audio_recorder.py             # 加 640ms 预缓冲（Top 10 #2）
├── speech_recognizer.py          # 加 hotwords（Top 10 #9）
├── tts.py                        # 预处理 + stream + vol 修复（Top 10 #6, #7, #10）
└── interrupt_monitor.py          # memory 注入（Top 10 #5）

config.yaml                       # VAD 双阈值 + 平滑（Top 10 #3, #4）
```

### 6.3 参考资料
- OLV v1.2 Release Notes: https://open-llm-vtuber.github.io/en/blog/v1.2.0-release/
- OLV Pet Mode 文档: https://docs.llmvtuber.com/docs/user-guide/frontend/electron
- VTube Studio lipsync 社区范式: https://github.com/DenchiSoft/VTubeStudio/wiki/Lipsync
- 深度调研原始数据: 参见 conversation history 2026-04-16

---

## 7. Verification Addendum（2026-04-16，源码级验证）

为避免关键决策基于 agent 推断而非事实，这一节是手动读源码验证后的修正。按 9 个验证项组织。

### V1：Jarvis TTSPipeline 改造可行性 ✅ 验证通过

**第一版说**：要自己写 WAV chunk + RMS 代码（~30 行）
**实际是**：`TTSEngine.synth_to_file(text, emotion) -> (path, deletable)`（`core/tts.py:281`）**已是干净的"合成到文件"接口**，直接调。

要改造成"不本地播放"有两种方式：
- **简单方式**：adapter 绕开 `TTSPipeline`，每句调 `engine.synth_to_file()` 然后 ws 发送。现有 `ui/web/server.py:102` 就是这样做的
- **保留双线程 pipeline**：subclass `TTSPipeline`，重写 `_play_worker`（`core/tts.py:783-803`），把 `_engine._play_audio_file(filepath)` 换成 `send_to_websocket(filepath)`

**`abort()` 已返回未播放文本列表**（`core/tts.py:723-750`），adapter 的 `interrupt-signal` 可直接调。

**音频格式**：MiniMax/Edge/OpenAI 默认 MP3，Azure WAV。OLV 前端实际**接受 `data:audio/wav;base64,`**（`use-audio-task.ts:96`），但浏览器 `<Audio>` 对 MP3 宽容度高，**可能不需要转换**，实测为准。

### V2：handle_text + on_sentence ✅ 验证通过

**完全确认**：`jarvis.py:1153` `handle_text(text, session_id, on_sentence, emotion, user_id, user_name, user_role)` 专为 Web 设计，注释写"纯文本入口（Web 前端用，不走录音/TTS）"。

- `on_sentence(sentence, emotion="")` — 同步回调
- `chat_stream` 只支持 `provider in ("anthropic", "openai")`（`core/llm.py:618`）——xAI Grok 走 openai-compatible 兼容
- `_process_turn` 有**大量 self._last_* 副作用 + 共享锁**，**单会话**使用安全，多会话并发会竞态

### V3：OLV audio payload 必填字段 ✅ 验证通过

**源码**：`Open-LLM-VTuber-Web/src/renderer/src/services/websocket-handler.tsx`
```ts
addAudioTask({
  audioBase64: message.audio || '',
  volumes: message.volumes || [],
  sliceLength: message.slice_length || 0,
  displayText: message.display_text || null,
  expressions: message.actions?.expressions || null,
  forwarded: message.forwarded || false,
});
```

**最小可行 payload**：`{type:"audio", audio:"<base64-mp3-or-wav>"}` —— 其他字段有默认值，不会崩溃。lipsync / 表情是**增量功能**，不是必须。

### V4：OLV model_info schema ✅ 验证通过

**源码**：同 websocket-handler.tsx 的 `set-model-and-conf` handler
```ts
setPendingModelInfo(message.model_info);
if (message.model_info && !message.model_info.url.startsWith("http")) {
  message.model_info.url = baseUrl + message.model_info.url;
}
```

**真正必填**：只有 `model_info.url`（Live2D `.model3.json` 的路径）。其他字段（`emotionMap`、`kScale`、`initialXshift` 等）是 Live2D context 的可选参数，没有就用默认值。

**握手 4 条消息**中的 `conf_name`/`conf_uid`/`client_uid` 都有 `if (message.xxx)` 守卫，**可以发空字符串**。

### V5：声纹 / auth / 多用户 ✅ 验证通过

- `SpeakerVerifier.verify(audio)` — 只在**音频路径**（`jarvis.py:577` 和 InterruptMonitor 内）
- `handle_text` 默认 `user_role="owner"` —— 本机 Mac 桌面场景直接用 owner 权限 OK
- **发现**：Jarvis 已有 `remote/` 模块（`remote/protocol.py`），token 认证 + 13 个 OS 动作（`open_app`/`close_app`/`set_volume`/`screenshot`/`lock_screen`/`system_info`/`run_command`/`media_control`/`open_url`/`type_text`/`get_active_window`/`list_running_apps`/`get_volume`）——**"对小月说'打开浏览器'"的底层能力 100% 具备**，不用新写

### V6：Live2D 许可证 ✅ 验证通过

- 所有 7 个模型（Haru/hiyori_pro_zh/Mao/Murasame_Yukata/natori_pro_zh/Rice/Senko_Normals）**都已在 Jarvis 仓库** `ui/web/resources/`（通过 xiaozhi 软链过来）
- hiyori 和 natori 的 `ReadMe.txt` 明确是 **Live2D 官方示例模型**，普通用户/小规模企业**商用 OK**，中大规模企业内部试用限定
- 许可证链接：https://www.live2d.com/zh-CHS/download/sample-data/
- **个人 Mac 桌面使用：零问题**

### V7：frontend-playback-complete 实际行为 ⚠️ 有新发现

**源码**：`Open-LLM-VTuber-Web/src/renderer/src/hooks/utils/use-audio-task.ts:230-247`
```ts
useEffect(() => {
  const handleComplete = async () => {
    await audioTaskQueue.waitForCompletion();
    if (isMounted && backendSynthComplete) {
      sendMessage({ type: "frontend-playback-complete" });
      setBackendSynthComplete(false);
    }
  };
  handleComplete();
}, [backendSynthComplete, ...]);
```

**关键发现（新坑）**：
1. 前端发 `frontend-playback-complete` 的触发条件：server **必须先发 `backend-synth-complete`**，把前端的 `backendSynthComplete` 状态置为 `true`
2. 然后等 `audioTaskQueue.waitForCompletion()` 完成
3. **没有 timeout**：如果 server 从不发 `backend-synth-complete`，前端永远不会回 `frontend-playback-complete`
4. Server 端若阻塞等这个 event（`conversation_utils.py:173`），**死锁永久**

**Adapter 必须**：
- 每轮对话**必发** `backend-synth-complete`
- 加 server 侧 timeout（比如 `asyncio.wait_for(ack_event, timeout=30)`），即使前端没回也能继续下一轮

### V8：Jarvis 线程 / 事件循环模型 ✅ 验证通过

- Jarvis **纯 threading**，无 asyncio at core level（`_executor = ThreadPoolExecutor(max_workers=3)`，`jarvis.py:253`）
- `on_sentence` 回调**在 worker 线程同步调用**（从 LLM 流读出一句就调一次）
- FastAPI 是 asyncio，要从 worker 线程 push 到 ws，标准模式是 **`asyncio.run_coroutine_threadsafe(ws.send_json(...), loop)`**
- 这个模式**`ui/web/server.py:120` 已经在用了**，直接抄

### V9：Jarvis 现有 web/remote 基础设施 🎯 重大发现

**第一版完全没意识到**：Jarvis 不是"需要加 web 前端"的项目，而是**已经有**一整套：

```
ui/web/                                    # 完整 Live2D 浏览器前端
├── server.py         (249 行)            # FastAPI + SSE，handle_text 已集成
├── index.html
├── js/
│   ├── app.js
│   ├── core/
│   │   ├── audio/                        # player / recorder / stream-context
│   │   └── api-client.js                 # SSE client（serial queue 保证顺序）
│   ├── live2d/
│   │   ├── live2d.js                     # Live2DManager（模型加载、嘴型、情绪、手势）
│   │   ├── pixi.js                       # PixiJS 渲染
│   │   ├── cubism4.min.js                # Cubism 4 SDK
│   │   └── live2dcubismcore.min.js
│   ├── ui/controller.js                  # UIController
│   └── config/manager.js                 # localStorage 配置
├── css/
├── images/
└── resources/        → xiaozhi live2d models（7 个模型软链）

remote/                                    # Mac 系统远程控制（独立于 ui/web/）
├── protocol.py       # Token 认证 + 13 个 OS 动作定义
├── agent.py          # Mac 侧 agent 执行器
└── client.py         # Jarvis 侧 client
```

**已有的协议**：Jarvis 用的是 **HTTP POST `/api/chat` + SSE 流**，不是 WebSocket。前端 `api-client.js:96` 的 `sendTextMessage` 就是 POST + 解析 SSE events。

**已有的消息类型**（SSE events）：
- `event: sentence` — 每句：`{index, text, emotion, audio_url}`
- `event: log` — 后端日志回传
- `event: done` — 对话结束

**和 OLV 协议映射**：
| Jarvis SSE | OLV WebSocket | 映射难度 |
|---|---|---|
| `POST /api/chat` | `text-input` msg | 简单 |
| `event: sentence` `{audio_url}` | `audio` msg `{audio: base64}` | 简单（读文件 base64 即可） |
| `event: done` | `backend-synth-complete` + `conversation-chain-end` | 简单 |
| 无 | `frontend-playback-complete` | 需 timeout fallback |
| 无 | `interrupt-signal` → `_cancel_current()` | Jarvis 已有 API |
| `POST /api/asr` | `mic-audio-data` + `mic-audio-end` | 中等（streaming buffer 累积）|

---

### Verification 一句话汇总

| 项 | 第一版 | 修订 |
|---|---|---|
| V1 TTS 改造 | 要写 30 行 | `synth_to_file()` 已有，0 行 |
| V2 on_sentence | 推断存在 | 源码确认 |
| V3 audio payload | 7 个字段 | 只 2 个字段是最小可行集 |
| V4 model_info | 多字段未列 | 只需 url |
| V5 Auth | 未查 | `remote/` 已有完整 OS 动作栈 |
| V6 License | 未查 | 已在仓库，个人使用 OK |
| V7 sync lock | 说必须等 | **必加 timeout，否则死锁** |
| V8 Threading | 推断 | 源码确认，`ui/web/server.py` 已有 runnable pattern |
| V9 Web 基础设施 | 未查 | **Jarvis 已有完整浏览器 Live2D 栈** |

---

## 8. 修订后的最终建议

### 8.1 新出现的最优路径：**Path D — Electron 壳包 Jarvis 现有 web 前端**

**为什么 Path D 突然最优**：
1. Jarvis 已有完整的 Live2D 浏览器前端 + FastAPI 后端（V9 发现）
2. "把浏览器 UI 放进 Electron 并加 Pet Mode" 是**增量式改动**，不触碰任何现有代码
3. 所有架构决策已经定好：pixi + Cubism 4、SSE 协议、7 个模型、对话序列化队列
4. OLV 前端可选（Path B 作为第二前端仍可保留）

### 8.2 三条可执行路径

#### **Path D（推荐，独占方案）**：Electron 包裹现有 web 前端

**工作**：
- 新建 `desktop/` 目录（Electron 项目，独立 `package.json`）
- `desktop/main.js`（~150 行）：BrowserWindow 配置 `{transparent: true, frame: false, alwaysOnTop: true}`，加载 `http://localhost:8006`（jarvis 的 web server）
- `desktop/preload.js`（~50 行）：`contextBridge` 暴露 `toggleClickThrough()`/`setPetMode()` 等 API
- `desktop/menu.js`（~80 行）：右键菜单 + 托盘菜单（参考 OLV 的 `main/menu-manager.ts`，约 180 行可抄）
- 前端小改：`ui/web/js/ui/controller.js` 加 Pet Mode toggle（~30 行），触发时改 body class 做视觉调整
- 点击穿透：抄 OLV 的 **组件 hover 上报** pattern（V9 的 §4.2 结论）

**总工作量**：**~1 天**（包含调试）

#### **Path B（可选，日常使用 OLV dmg）**：新增 WebSocket 端点

**工作**：
- `ui/olv/websocket_server.py`（~250-350 行）：在现有 FastAPI app 里加 `/client-ws` 路由
- 复用 `handle_text` + `synth_to_file` 已有逻辑（`ui/web/server.py:94-144` 90% 可抄）
- 加 `backend-synth-complete` + timeout 机制（V7 坑）
- 握手发伪造的 `model_info`（URL 指向 `http://localhost:8006/resources/natori_pro_zh/runtime/...`）

**总工作量**：**~0.5 天**（因为 `ui/web/server.py` 提供了 80% 的代码模板）

**Path D + B 叠加**：~1.5 天全做完，两个前端都能用

#### **Path C（废弃）**：自建 Electron + 重写 Live2D 集成
- 原本估 1-2 周
- 现在看 Path D 已经覆盖其价值，**不再推荐**

### 8.3 OS 动作能力（用户需求"对小月说打开浏览器"）

**已有**：`remote/protocol.py` 定义 13 个动作、`remote/agent.py` 执行器、`remote/client.py` 客户端

**待做**：
- 把 `remote/client.py` 的动作做成 Jarvis tools 注册到 `ToolRegistry`（这样 LLM 能调用）
- 大约 **~50 行** wrapper 代码

### 8.4 语音参数借鉴（§2 不变）

Top 10 可抄的参数依然有效，但**尚未实测 benchmark**。Phase 2 应当：
1. 测 Jarvis 当前首字延迟基线（多个场景）
2. 改 4 个最影响的参数（首句逗号切 / 预缓冲 / VAD 双阈值 / VAD 平滑）
3. 再次测量，验证预期收益

---

## 9. 执行计划（修订版）

**假设用户确认走 Path D**：

| Phase | 任务 | 时间 | 验收 |
|---|---|---|---|
| 1a | 建 `desktop/` Electron 项目骨架 | 2h | `npm start` 打开一个加载 localhost:8006 的透明窗 |
| 1b | 实现 Pet Mode 切换 + 点击穿透 + 置顶 | 3h | 小月悬浮桌面，轮廓外点击穿透到底下 app |
| 1c | 右键菜单 + 托盘菜单 | 2h | 切 Live/Pet、打断、退出都能用 |
| 1d | 调试 + 打 dmg | 1h | `open desktop/dist/jarvis-desktop.dmg` 可装 |
| **Phase 1 合计** | — | **~1 天** | 整套 Path D 可用 |
| 2a | `remote/client.py` 封装为 ToolRegistry tools | 2h | LLM 能调用 `open_url` 等 |
| 2b | 跑 system test 端到端 | 1h | "打开 Google" → 真的开了 |
| **Phase 2 合计** | — | **~0.5 天** | OS 动作全通 |
| 3（可选）| `ui/olv/websocket_server.py` — OLV dmg 兼容 | ~0.5 天 | OLV dmg 能连上 Jarvis 正常对话 |
| 4（可选）| 语音参数借鉴（Top 4）+ benchmark | ~1 天 | 首字延迟实测下降 |

---

**报告状态**：Verification Addendum 完成
**下一步**：等用户看完 §8 的修订建议，确认走 Path D 还是其他路径。实际代码改动由其他 agent 执行。
