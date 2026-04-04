# Live2D 虚拟角色聊天前端 — 迁移指南

## 来源

从 `xiaozhi-esp32-server/main/xiaozhi-server/test/` 提取的独立前端页面，原始项目是小智 ESP32 语音助手的 Web 测试客户端。

## 目标项目

Jarvis — Python 私人语音管家（详见 `/Users/alllllenshi/Projects/jarvis/CLAUDE.md`）。
技术栈：Python 3.11 + SenseVoice ASR + GPT-4o-mini LLM + MiniMax TTS + Porcupine 唤醒词。

---

## 一、文件结构（全部在 `ui/web/` 下）

```
ui/web/
├── test_page.html              # 主页面入口（需重命名为 index.html）
├── favicon.ico
├── css/
│   ├── test_page.css           # 全部样式（~700行）
│   └── bg.png                  # 默认背景纹理
├── images/
│   ├── 1.png                   # 背景图1（粉色卧室）
│   ├── 2.png                   # 背景图2
│   └── 3.png                   # 背景图3
├── js/
│   ├── app.js                  # 主应用入口（App 类）
│   ├── ui/
│   │   ├── controller.js       # UI 控制器（UIController 类，事件绑定、状态管理）
│   │   └── background-load.js  # 背景图加载检测（IIFE）
│   ├── live2d/
│   │   ├── live2d.js           # Live2DManager 类（模型加载、嘴部动画、情绪动作、手势交互）
│   │   ├── pixi.js             # PixiJS 渲染引擎（第三方库，~500KB）
│   │   ├── cubism4.min.js      # Live2D Cubism 4.0 SDK（第三方库）
│   │   └── live2dcubismcore.min.js  # Cubism 核心运行时（第三方库）
│   ├── core/
│   │   ├── audio/
│   │   │   ├── player.js       # AudioPlayer 类（Opus 解码 → Web Audio 播放）
│   │   │   ├── recorder.js     # AudioRecorder 类（麦克风 → Opus 编码 → WebSocket 发送）
│   │   │   ├── opus-codec.js   # Opus 编解码器初始化（依赖 libopus.js WASM）
│   │   │   └── stream-context.js  # StreamingContext 类（音频流缓冲调度）
│   │   ├── network/
│   │   │   ├── websocket.js    # WebSocketHandler 类（消息路由、TTS/STT/LLM/MCP 处理）
│   │   │   └── ota-connector.js  # OTA 连接器（POST OTA 获取 WebSocket URL + Token）
│   │   └── mcp/
│   │       └── tools.js        # MCP 工具管理（注册、编辑、执行、mock 响应）
│   ├── config/
│   │   ├── manager.js          # 配置管理（localStorage 读写 MAC/ClientId/OTA URL）
│   │   └── default-mcp-tools.json  # 默认 MCP 工具定义（4 个工具）
│   └── utils/
│       ├── logger.js           # 日志函数（写入 DOM #logContainer 或 console.log）
│       ├── libopus.js          # Opus WASM 编译库（Emscripten 产物，~200KB）
│       └── blocking-queue.js   # BlockingQueue 类（异步生产者-消费者队列）
└── resources/                  # Live2D 模型资源（~96MB，7 个模型）
    ├── hiyori_pro_zh/          # 春日（默认模型）
    ├── natori_pro_zh/          # 名取
    ├── Mao/                    # 虹色猫
    ├── Haru/                   # 春
    ├── Rice/                   # 莱斯
    ├── Murasame_Yukata/        # 浴衣少女
    └── Senko_Normals/          # 仙狐小姐
```

---

## 二、架构概览

### 技术选型
- **纯原生 JS**（ES Modules），无构建步骤，无框架依赖
- **Live2D**：PixiJS + Cubism 4.0 SDK（`pixi.js` + `cubism4.min.js` + `live2dcubismcore.min.js`）
- **音频**：Web Audio API + Opus WASM（`libopus.js` 由 Emscripten 编译）
- **通信**：WebSocket（二进制 Opus 帧 + JSON 控制消息）
- **存储**：localStorage（配置、MCP 工具、模型选择、背景选择）

### 数据流

```
用户操作 → UIController → WebSocketHandler → 后端服务器
                                     ↓
                              JSON 消息分发:
                              ├── type: "hello"  → 握手确认
                              ├── type: "stt"    → 语音识别结果（显示用户消息）
                              ├── type: "llm"    → LLM 回复（显示 AI 消息 + 情绪动作）
                              ├── type: "tts"    → TTS 控制（start/sentence_start/sentence_end/stop）
                              ├── type: "mcp"    → MCP 工具调用/列表
                              └── binary         → Opus 音频帧（解码后播放）

录音流:  麦克风 → AudioWorklet → PCM Int16 → Opus 编码 → WebSocket 二进制帧
播放流:  WebSocket 二进制帧 → BlockingQueue → Opus 解码 → Web Audio BufferSource → 扬声器
```

### 单例模式
以下模块全部使用单例：
- `getAudioPlayer()` → AudioPlayer
- `getAudioRecorder()` → AudioRecorder
- `getWebSocketHandler()` → WebSocketHandler
- `uiController` → UIController（直接导出实例）
- `window.chatApp` → App（全局暴露）

---

## 三、核心模块详解

### 1. WebSocket 通信协议

#### 连接流程
```
1. 用户点击"拨号" → UIController.handleConnect()
2. POST OTA 地址 → 获取 {websocket: {url, token}} 
3. new WebSocket(url + ?authorization=Bearer+token&device-id=X&client-id=Y)
4. onopen → sendHelloMessage({type:"hello", device_id, device_name, device_mac, token, features:{mcp:true}})
5. 收到 {type:"hello", session_id} → 握手成功
```

#### OTA 请求格式（ota-connector.js）
```json
POST {otaUrl}
Headers: {"Content-Type":"application/json", "Device-Id":"...", "Client-Id":"..."}
Body: {
  "version": 0,
  "application": {"name":"xiaozhi-web-test", "version":"1.0.0", ...},
  "board": {"type": "{deviceName}", "mac": "{deviceMac}", ...},
  "mac_address": "{deviceMac}"
}
Response: {"websocket": {"url":"wss://...", "token":"Bearer ..."}}
```

#### 消息类型（websocket.js handleTextMessage）
| type | 方向 | 说明 |
|------|------|------|
| hello | 双向 | 握手。客户端发送设备信息，服务端返回 session_id |
| stt | 服务端→客户端 | 语音识别结果，{text: "用户说的话"} |
| llm | 服务端→客户端 | LLM 回复，{text: "...", emotion: "happy"} |
| tts | 服务端→客户端 | TTS 控制，state: start/sentence_start/sentence_end/stop |
| mcp | 双向 | MCP 工具交互（JSON-RPC 2.0 格式）|
| listen | 客户端→服务端 | 文本输入，{state:"detect", text:"用户输入"} |
| abort | 客户端→服务端 | 打断 TTS，{session_id, reason:"wake_word_detected"} |
| binary | 双向 | Opus 音频帧（ArrayBuffer） |

### 2. MCP 工具系统（mcp/tools.js）

#### 工具定义格式
```json
{
  "name": "tool_name",
  "description": "工具描述",
  "inputSchema": {
    "type": "object",
    "properties": { "param1": { "type": "string", "description": "..." } },
    "required": ["param1"]
  },
  "mockResponse": { "success": true, "message": "支持 ${param1} 模板变量替换" }
}
```

#### MCP JSON-RPC 协议
```
服务端 → 客户端: {type:"mcp", payload:{method:"initialize", id:1, params:{capabilities:{vision:{url,token}}}}}
客户端 → 服务端: {type:"mcp", payload:{jsonrpc:"2.0", id:1, result:{protocolVersion:"2024-11-05", capabilities:{tools:{}}, serverInfo:{name,version}}}}

服务端 → 客户端: {type:"mcp", payload:{method:"tools/list", id:2}}
客户端 → 服务端: {type:"mcp", payload:{jsonrpc:"2.0", id:2, result:{tools:[...]}}}

服务端 → 客户端: {type:"mcp", payload:{method:"tools/call", id:3, params:{name:"tool_name", arguments:{...}}}}
客户端 → 服务端: {type:"mcp", payload:{jsonrpc:"2.0", id:3, result:{content:[{type:"text", text:"..."}], isError:false}}}
```

#### 默认工具列表
1. **self_camera_take_photo** — 拍照（真实执行：调用浏览器摄像头 → POST 图片到 vision API）
2. **self.get_device_status** — 获取设备状态（mock: 音量/亮度/电量/网络）
3. **self.audio_speaker.set_volume** — 设置音量（mock，支持 `${volume}` 模板）
4. **self.screen.set_brightness** — 设置亮度（mock，支持 `${brightness}` 模板）

#### 关键：mock → 真实调用改造点
在 `executeMcpTool()` 中，当前除拍照外全部返回 mockResponse。要对接 Jarvis 真实能力：
- 替换 mockResponse 逻辑为 HTTP/WebSocket 调用 Jarvis Python 后端
- 或在 WebSocket 消息层由 Jarvis 后端处理工具调用，前端只做 UI 展示

### 3. 音频系统

#### 播放（player.js + stream-context.js）
- Opus 二进制帧通过 WebSocket 到达 → `enqueueAudioData()` 入队
- `BlockingQueue` 异步缓冲 6 包后开始播放
- Opus WASM 解码 → Int16 → Float32 → `AudioBufferSourceNode.start(scheduledTime)` 精确调度
- `StreamingContext.analyser` 节点供 Live2D 嘴部动画读取频谱数据

#### 录音（recorder.js + opus-codec.js）
- `getUserMedia({audio})` → `AudioWorkletNode` 采集 PCM
- 每 960 样本（60ms@16kHz）一帧 → Opus 编码 → WebSocket 发送二进制帧
- 停止时发送空 Uint8Array(0) 作为结束信号
- 回退方案：`ScriptProcessorNode`（AudioWorklet 不可用时）

#### Opus WASM（libopus.js）
- Emscripten 编译的 Opus 库，暴露全局 `Module` / `Module.instance`
- 使用的函数：`_opus_encoder_init/encode/get_size`、`_opus_decoder_init/decode/get_size`、`_malloc/_free`
- 参数：16kHz 采样率、单声道、VOIP 模式、16kbps 码率

### 4. Live2D 系统（live2d/live2d.js）

#### 模型配置
```javascript
modelFileMap = {
  'hiyori_pro_zh': { file: 'hiyori_pro_t11.model3.json', subdir: 'runtime/' },
  'natori_pro_zh': { file: 'natori_pro_t06.model3.json', subdir: 'runtime/' },
  'Mao': { file: 'Mao.model3.json', subdir: '' },
  'Haru': { file: 'Haru.model3.json', subdir: '' },
  'Rice': { file: 'Rice.model3.json', subdir: '' },
  'Murasame_Yukata': { file: 'Murasame_Yukata.model3.json', subdir: '' },
  'Senko_Normals': { file: 'senko.model3.json', subdir: '' }
}
```

#### 嘴部动画
- TTS 开始时 `startTalking()` → 读取 `StreamingContext.analyser` 频谱数据 → 映射到 `ParamMouthOpenY` 参数
- TTS 停止时 `stopTalking()`
- 每个模型有独立的 `mouthAmplitude` 和 `mouthThresholds` 配置

#### 情绪动作映射
```javascript
emotionToActionMap = {
  'happy': 'FlickUp',       // 开心
  'laughing': 'FlickUp',    // 大笑
  'sad': 'FlickDown',       // 伤心
  'angry': 'Tap@Body',      // 生气
  'surprised': 'Tap',       // 惊讶
  'neutral': 'Flick',       // 平常
  'default': 'Flick@Body'   // 默认
}
```
LLM 返回 `emotion` 字段时，通过 `triggerEmotionAction()` 触发对应 motion。

#### 交互手势
- 单击头部/身体 → `Tap` / `Tap@Body` motion
- 双击头部/身体 → `Flick` / `Flick@Body` motion
- 上下滑动 → `FlickUp` / `FlickDown` motion
- 自定义命中区域判定：模型可见矩形 relY ≤ 0.15 = Head, ≤ 0.23 = Face, else = Body

### 5. UI 控制器（ui/controller.js）

#### 状态管理
- 连接状态：`updateConnectionUI(isConnected)` → 顶部状态点/文字
- 拨号按钮：`updateDialButton(isConnected)` → 图标切换（电话/挂断）、启用/禁用录音/摄像头按钮
- 录音按钮：`updateRecordButtonState(isRecording)` → 文字切换（录音/录音中）
- 背景切换：`switchBackground()` → 3 张背景图轮换，存 localStorage
- 模型切换：`switchLive2DModel()` → 调用 `Live2DManager.switchModel()`

#### 事件绑定
- 设置按钮 → 打开设置模态框（3 个 Tab：设备配置 / MCP 工具 / 数字人皮肤）
- 拨号按钮 → 连接/断开 WebSocket
- 摄像头按钮 → 开启/关闭摄像头（需先连接）
- 录音按钮 → 开始/停止录音（需先连接且麦克风可用）
- 文本输入 → Enter 发送文本消息
- 关闭设置时 → 自动保存配置到 localStorage

### 6. 配置管理（config/manager.js）

#### localStorage 键值
| Key | 说明 |
|-----|------|
| xz_tester_deviceMac | 设备 MAC（首次自动随机生成） |
| xz_tester_deviceName | 设备名称 |
| xz_tester_clientId | 客户端 ID |
| xz_tester_otaUrl | OTA 服务器地址 |
| xz_tester_wsUrl | WebSocket 地址（从 OTA 获取） |
| xz_tester_vision | 视觉分析配置 JSON（{url, token}） |
| backgroundIndex | 当前背景图索引（0-2） |
| live2dModel | 当前 Live2D 模型名称 |
| mcpTools | MCP 工具列表 JSON |

---

## 四、迁移到 Jarvis 的关键改造点

### 必须改的
1. **OTA 连接逻辑**（ota-connector.js）：Jarvis 不使用 OTA，需改为直接连接 Jarvis WebSocket 服务端
2. **WebSocket 协议适配**（websocket.js）：根据 Jarvis 后端实际的消息格式调整 `handleTextMessage()`
3. **test_page.html 重命名**为 `index.html`
4. **页面标题**从"小智服务器测试页面"改为 Jarvis 相关
5. **配置面板**中的"OTA 服务器地址"改为 Jarvis WebSocket 地址直连

### 建议改的
1. **MCP 工具**：将 mock 工具替换为 Jarvis 的真实 skills（灯光控制、天气、提醒等）
2. **背景图**：替换为 Jarvis 主题的背景
3. **localStorage 键名前缀**：从 `xz_tester_` 改为 `jarvis_`
4. **serverInfo**：`xiaozhi-web-test` → `jarvis-web-client`

### 可选改的
1. **Live2D 模型**：可保留或替换为自定义模型
2. **摄像头功能**：如 Jarvis 不需要视觉分析可移除
3. **Opus 编解码**：如 Jarvis 使用其他音频格式需替换
4. **日志系统**：logger.js 目前写入不存在的 `#logContainer`，仅输出到 console

---

## 五、启动方式

```bash
cd /Users/alllllenshi/Projects/jarvis/ui/web
python -m http.server 8006
# 浏览器打开 http://localhost:8006/test_page.html
```

注意：必须通过 HTTP 服务器访问，`file://` 协议会导致 ES Modules 和 CORS 问题。

---

## 六、第三方库许可证

| 库 | 用途 | 来源 |
|----|------|------|
| PixiJS | 2D 渲染引擎 | MIT License |
| Live2D Cubism SDK | Live2D 模型渲染 | Live2D Open Software License |
| libopus (Emscripten) | Opus 音频编解码 | BSD License |
| Live2D 模型资源 | 7 个角色模型 | Live2D 免费可发布参考素材 |

---

## 七、Jarvis 后端集成建议

Jarvis 已有的架构：
```
麦克风 → 唤醒词 → 录音(VAD) → ASR → 意图路由 → LLM → TTS → 扬声器
```

Web 前端集成后的架构：
```
浏览器麦克风 → Opus 编码 → WebSocket → Jarvis Python 后端
                                              ↓
                                    ASR → 意图路由 → LLM → TTS
                                              ↓
                              WebSocket ← JSON 控制消息 + Opus 音频帧
                                    ↓
                              浏览器 Opus 解码 → 播放 + Live2D 嘴部动画
```

需要在 Jarvis 后端新增：
1. WebSocket 服务端（处理音频流 + JSON 消息）
2. Opus 编解码支持（`opuslib` Python 包）
3. 消息协议适配层（匹配前端期望的 hello/stt/llm/tts/mcp 消息格式）
