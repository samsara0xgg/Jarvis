# Live2D Web 前端集成设计

## 概述

将小智 ESP32 项目的 Live2D 虚拟角色聊天前端迁移到 Jarvis，用 HTTP API + SSE 替代原有的 WebSocket + Opus 方案。复用 Jarvis 现有的意图路由、LLM 流式输出、TTS 合成、ASR 识别等全部核心模块。

## 架构

```
浏览器                              Jarvis 后端 (FastAPI)
──────                              ────────────────────
文字输入 ─── POST /api/chat ──────→ IntentRouter.route()
                                      ├─ 本地: LocalExecutor → 技能结果
                                      └─ 云端: LLMClient.chat_stream()
         ←── SSE 流 ─────────────────  每句: TTS 合成 → 推送 sentence 事件
                                        │
显示文字 + 播放音频 + Live2D 动画 ←──┘

录音 WAV ─── POST /api/asr ──────→ SpeechRecognizer.transcribe()
         ←── JSON {text, emotion} ──

拨号按钮 ─── POST /api/session ───→ 创建会话，返回 session_id
挂断按钮 ─── DELETE /api/session ──→ 结束会话
```

## 后端 API

### 新增文件：`ui/web/server.py`

FastAPI 应用，复用 JarvisApp 现有模块。

### 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 静态文件（index.html + 前端资源） |
| GET | `/api/health` | 健康检查 `{status: "ok"}` |
| POST | `/api/session` | 创建会话，返回 `{session_id}` |
| DELETE | `/api/session/{id}` | 结束会话 |
| POST | `/api/chat` | 文字聊天，返回 SSE 流 |
| POST | `/api/asr` | 浏览器录音 → ASR 识别 |
| GET | `/api/audio/{name}` | TTS 合成的 MP3 文件 |

### POST /api/chat

请求：
```json
{"text": "今天天气怎么样", "session_id": "uuid"}
```

响应（SSE 流，音画同步 — 每句等 TTS 就绪再推送）：
```
event: sentence
data: {"index": 0, "text": "今天天气晴朗", "emotion": "happy", "audio_url": "/api/audio/a1b2c3.mp3"}

event: sentence
data: {"index": 1, "text": "最高温25度", "emotion": "neutral", "audio_url": "/api/audio/d4e5f6.mp3"}

event: done
data: {}
```

后端处理流水线：
1. `on_sentence` 回调不阻塞 LLM 流 — 句子进 TTS 合成队列
2. TTS 合成线程按序处理：`TTSEngine.synth_to_file(sentence, emotion)` → 生成 MP3
3. 合成完毕后推送 SSE 事件（text + audio_url 一起出，音画同步）
4. LLM 继续产出后续句子，与 TTS 合成并行

### POST /api/asr

请求：`multipart/form-data`，字段 `audio` = WAV blob（16kHz mono）

响应：
```json
{"text": "用户说的话", "emotion": "HAPPY"}
```

### POST /api/session

响应：
```json
{"session_id": "uuid", "status": "connected"}
```

### DELETE /api/session/{id}

响应：
```json
{"status": "disconnected"}
```

## 后端实现

### JarvisApp 新增方法：`handle_text()`

从 `_handle_utterance_inner()` 提取 step 4-9 的文本处理逻辑，跳过录音/ASR/声纹验证：

```python
def handle_text(self, text: str, session_id: str = "_web",
                on_sentence=None, emotion: str = "") -> str:
    """文本输入管线 — 复用路由/技能/LLM/记忆全部逻辑。"""
    user_id = "default_user"
    user_name = "用户"
    user_role = "owner"
    # 4. 加载对话历史 + 记忆查询
    # 4b. Level 1 直接回答
    # 4c. 记忆存储快捷方式
    # 4d. 学习意图检测
    # 5. 关键词触发
    # 6. 意图路由 (local/cloud)
    # 7. 云端 LLM 流式输出 (on_sentence 回调)
    # 8. 保存对话历史 + 记忆提取
```

### Web Server 结构

```python
# ui/web/server.py
class WebPipeline:
    """FastAPI + JarvisApp 桥接层。"""

    def __init__(self, config):
        self.app = JarvisApp(config)
        self.tts = self.app._tts_engine  # 复用 TTS
        self.asr = self.app.speech_recognizer  # 复用 ASR
        self.audio_dir = Path("ui/web/audio_cache")
        self.sessions: dict[str, dict] = {}

    async def chat_sse(self, text, session_id):
        """调用 handle_text()，桥接同步回调到 SSE 流。"""
        queue = asyncio.Queue()
        tts_pool = ThreadPoolExecutor(max_workers=2)

        def on_sentence(sentence):
            # TTS 合成（同步，在线程池中）
            audio_path = self.tts.synth_to_file(sentence, emotion)
            audio_name = Path(audio_path).name
            # 复制到 audio_cache 目录
            shutil.copy2(audio_path, self.audio_dir / audio_name)
            Path(audio_path).unlink(missing_ok=True)
            # 推送合并事件
            asyncio.run_coroutine_threadsafe(
                queue.put({"text": sentence, "audio_url": f"/api/audio/{audio_name}"}),
                loop
            )

        # JarvisApp.handle_text() 在线程中运行
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.app.handle_text(
            text, session_id, on_sentence=on_sentence
        ))
        await queue.put(None)  # sentinel

        # yield SSE events
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
```

### TTS 文件管理

- 合成文件存 `ui/web/audio_cache/`，UUID 文件名
- 服务启动时清空目录
- 后台每 5 分钟清理 > 10 分钟的文件

### 复用的现有模块

| 模块 | 用途 |
|------|------|
| `IntentRouter` | Groq/DeepSeek/Ollama 意图路由 |
| `LLMClient.chat_stream()` | 流式 LLM + 逐句回调 |
| `TTSEngine.synth_to_file()` | TTS 合成到 MP3 文件 |
| `SpeechRecognizer.transcribe()` | SenseVoice ASR |
| `LocalExecutor` | 本地技能执行（灯光/天气/时间等） |
| `SkillRegistry` | 技能注册 + tool definitions |
| `ConversationStore` | 对话历史 |
| `MemoryManager` | 记忆查询 + 存储 |
| `DirectAnswerer` | Level 1 直接回答 |
| `LearningRouter` | 学习意图检测 |
| `AutomationRuleManager` | 关键词触发规则 |

### 启动方式

```bash
python -m ui.web.server                    # http://localhost:8006
python -m ui.web.server --port 8006 --host 0.0.0.0
```

加载 `config.yaml`，初始化 JarvisApp 所有模块。

## 前端改动

### 文件操作清单

**删除（7 个文件）：**
- `js/core/network/ota-connector.js` — 无 OTA
- `js/core/audio/opus-codec.js` — 无 Opus
- `js/core/mcp/tools.js` — 无 MCP
- `js/config/default-mcp-tools.json` — 无 MCP
- `js/utils/libopus.js` — 无 Opus WASM（~200KB）
- `js/utils/blocking-queue.js` — 不需要

**重写（3 个文件）：**
- `js/core/network/websocket.js` → `js/core/api-client.js`
  - `connect()` → POST /api/session
  - `disconnect()` → DELETE /api/session
  - `sendTextMessage()` → POST /api/chat (SSE)
  - `sendAudio()` → POST /api/asr
- `js/core/audio/player.js`
  - 从 Opus 解码播放器改为 URL 音频队列播放器
  - 用 Web Audio API：fetch MP3 → decodeAudioData → AudioBufferSourceNode
  - 保留 AnalyserNode 供 Live2D 嘴部动画使用
- `js/core/audio/recorder.js`
  - 从 Opus 编码 + WebSocket 发送改为 AudioWorklet → 16kHz PCM → WAV blob
  - 录完整段后 POST /api/asr

**修改（5 个文件）：**
- `index.html`（已从 test_page.html 重命名）
  - 标题：`小智服务器测试页面` → `小贾 | Jarvis`
  - 删除 Opus 脚本标签：`<script src="js/utils/libopus.js">`
  - 删除 MCP 工具编辑模态框（#mcpToolModal、#mcpPropertyModal）
  - 设置面板：删除 MCP 工具 tab，删除 OTA 地址输入，改为 Jarvis 服务器地址
  - file:// 警告文字更新
- `js/app.js`
  - 删除 Opus/MCP 初始化
  - 改为导入 api-client
  - `xz_tester_vision` → 删除视觉相关代码
- `js/ui/controller.js`
  - 拨号按钮：改为调用 api-client.connect()/disconnect()
  - 删除 OTA URL 检查逻辑
  - 删除 MCP 工具管理方法
  - handleConnect()：改为 POST /api/session
- `js/config/manager.js`
  - localStorage 前缀：`xz_tester_` → `jarvis_`
  - 删除 OTA URL 字段
  - 新增 server URL 字段（默认 `http://localhost:8006`）
  - 新增 session_id 字段
- `js/core/audio/stream-context.js`
  - 简化：从新 player 的 AudioContext 接 AnalyserNode
  - 删除 Opus 相关缓冲逻辑

**小改（1 个文件）：**
- `js/live2d/live2d.js`
  - `startTalking()`：音频源从 StreamingContext.analyser 切到新 player 的 AnalyserNode
  - 接口不变，只是 analyser 数据源变了

**保留不动（5 个文件）：**
- `js/live2d/pixi.js` — PixiJS 渲染引擎
- `js/live2d/cubism4.min.js` — Cubism 4.0 SDK
- `js/live2d/live2dcubismcore.min.js` — Cubism 核心运行时
- `js/ui/background-load.js` — 背景图加载
- `js/utils/logger.js` — 日志

**CSS：**
- `css/test_page.css` — 小改：删除 MCP 相关样式，保留其余

### 前端数据流（改造后）

```
拨号 → POST /api/session → session_id → 状态变绿，启用录音/聊天
  │
  ├─ 文字输入 → POST /api/chat {text, session_id}
  │              ← SSE: sentence {text, emotion, audio_url} × N
  │              ← SSE: done
  │              → 显示文字 + 播放音频 + Live2D 情绪/嘴动
  │
  ├─ 录音按钮 → AudioWorklet 采集 16kHz PCM → WAV blob
  │            → POST /api/asr {audio: blob}
  │            ← {text, emotion}
  │            → 自动调用 /api/chat
  │
  └─ 挂断 → DELETE /api/session → 停录音，状态变灰
```

### localStorage 键值（改造后）

| Key | 说明 |
|-----|------|
| `jarvis_serverUrl` | Jarvis 服务器地址（默认 http://localhost:8006） |
| `jarvis_sessionId` | 当前会话 ID |
| `backgroundIndex` | 当前背景图索引 |
| `live2dModel` | 当前 Live2D 模型名称 |

### 设置面板（改造后）

2 个 Tab（原来 3 个）：
1. **连接配置** — 服务器地址
2. **数字人皮肤** — 模型选择 + 背景切换（保持不变）

## 不做的

- Opus 编解码 — 用 WAV/MP3 替代
- MCP 工具系统 — Jarvis 有自己的 skill 体系
- 摄像头/视觉分析 — 暂不需要
- 声纹验证 — Web 端不实用
- WebSocket 长连接 — HTTP + SSE 足够

## 依赖

### Python 新增

- `fastapi` — Web 框架
- `uvicorn` — ASGI 服务器
- `python-multipart` — multipart 表单解析（ASR 上传）
- `soundfile` — WAV 文件读取

### 前端

无新增依赖。删除 libopus.js WASM。
