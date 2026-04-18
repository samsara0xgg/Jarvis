# 任务 2 · 开源项目/产品先例："atomic tools + LLM orchestration"

*2026-04-15*

---

## 1. Open Interpreter

**URL:** https://github.com/openinterpreter/open-interpreter
**Stars:** ~63k | **License:** AGPL-3.0 | **Latest:** v0.4.2 (2024-10, prerelease) | **Last push:** 2026-02

### 架构

不是固定 tool schema，而是**自由代码生成 + 执行**：

```
用户 NL → LLM (via LiteLLM, 100+ provider)
  → 生成 markdown code block (python/js/shell)
  → 直接在用户环境执行 (full user permissions)
  → stdout/stderr 回传 LLM → 迭代
```

两层能力：

| 层 | 内容 |
|---|---|
| **Code Kernel** | Python (persistent state), JS/Node, Shell — LLM 自选语言 |
| **Computer API (OS Mode)** | `computer.display` (screenshot), `computer.mouse` (OCR click), `computer.keyboard`, `computer.clipboard`, `computer.browser`, `computer.mail/sms/contacts/calendar` (macOS only) |

Computer API 用 OCR 驱动任意 GUI 应用，这是它与 Claude Code 的差异化点。

### 实际效果

- **简单任务 (数据分析、文件操作、shell 自动化):** GPT-4o/Claude 下效果好
- **多步 agentic chain:** 脆弱，容易 loop/stuck/幻觉中间状态
- **GUI 控制 (OS Mode):** Demo 级别，依赖分辨率/OCR/vision model 质量
- **Local models (Ollama):** 质量显著下降

### 安全问题 (核心短板)

- 以用户权限执行任意代码，唯一保护是 confirmation prompt (`--auto-run` 直接绕过)
- Safe mode 用 semgrep 静态扫描，Simon Willison 批评："sandbox 才是正解，但 Python sandbox 仍是难题"
- Docker 支持标记 experimental

### Jarvis 启示

- 自由组合模型 (LLM 写代码而非调固定 tool) 的天花板和地板都很清楚
- Computer API 的 OCR-based GUI 控制思路有趣但不适合 headless RPi
- 开发动力自 2024 末明显放缓，OpenHands (69.5k stars, SWE-bench 77.6%) 已超越

---

## 2. Computer Use Agents

### 2.1 Anthropic Computer Use

**URL:** https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool
**类型:** API 能力 (非产品)

- 纯 screenshot-in / action-out 循环，用户自建执行 loop
- Atomic actions: `click(x,y)`, `type()`, `key()`, `scroll`, `screenshot` — 坐标制
- 需要运行桌面环境 (物理或 Xvfb)
- **GA** in API，每步 ~5-10s (screenshot roundtrip)，适合无 API 的 legacy GUI

### 2.2 OpenAI Operator / CUA

**URL:** https://operator.chatgpt.com | API: `computer-use-preview` via Responses API

- **Operator** = 托管沙箱浏览器产品; **CUA** = 底层 vision+RL model
- Observe → Reason → Act 循环，GPT vision + RL
- Atomic: click, type, scroll, keyboard, multi-tab
- 安全: Takeover Mode (密码/支付), prompt-injection 检测
- SDK v1.2 (2026-03) 加了 kernel-level mouse control
- **可用性:** Pro ($200/mo), rolling out to Plus/Team; API via Responses API
- <1M WAU (Wired 数据)，采用率还在早期

### 2.3 Google Project Mariner

**URL:** https://deepmind.google/models/project-mariner/

- Gemini 2.0 驱动, "pixels-to-action" + Observe→Plan→Act
- 浏览器专用 (非全桌面), WebVoyager 83.5% success rate
- "Transparent Reasoning" 侧边栏实时显示规划步骤
- Teach & Repeat: 观察工作流后自动重放
- **2026-03 Google 重新分配部分 Mariner 团队**，功能正被吸收进 Gemini Agent Mode
- 仅 AI Ultra ($250/mo, US only)

### 架构共性

**三者核心循环完全相同:** screenshot/视觉观察 → LLM 规划下一步 → atomic action → 重复。没有 "intent-level" API，全在原始像素/输入层操作，LLM 负责所有高层任务分解。

| | Anthropic | OpenAI Operator | Google Mariner |
|---|---|---|---|
| 范围 | 全桌面 | 浏览器 | 浏览器 |
| 模型 | Claude | CUA (GPT vision+RL) | Gemini 2.0 |
| 可用性 | API (GA) | Pro/Plus/API | Ultra only |
| 状态 | 稳定 | 扩张中 | 被吸收进 Gemini |

### Jarvis 启示

- Computer Use 方向对 headless RPi 无直接意义
- 但 "atomic action + LLM planning" 的循环模式是通用的 — 用 `execute_service()` / skill 调用替代 `click(x,y)` 即可

---

## 3. Home Assistant + LLM

### 3.1 HA Voice Pipeline (Assist)

**Docs:** https://developers.home-assistant.io/docs/voice/pipelines

```
Wake word → STT → Conversation Agent (LLM slot) → TTS → Audio
```

- 每阶段可替换, Wyoming 协议连接本地 STT/TTS/wake
- **LLM API** (`homeassistant.components.conversation`) 将 HA service 暴露为 tool-call schema
- Production 级别，HA core 自带
- 单次 pipeline = 单次 request-response，无内建多步规划

### 3.2 Extended OpenAI Conversation (最佳参考)

**Repo:** https://github.com/jekalmin/extended_openai_conversation
**Stars:** ~1k+ | **Active**

在内建 OpenAI 集成基础上增加:

| 函数 | 能力 |
|---|---|
| `execute_service` | 调任意 HA service |
| `create_automation` | LLM 生成并保存自动化 |
| `get_history` | 获取实体历史 |
| `rest` / `scrape` | 外部 API / 网页 |
| `composite` | 单 turn 内链式多调用 |
| `template` | 渲染 Jinja2 模板 |

YAML 配置函数 schema → 注入 OpenAI function definitions → LLM 决定调用 → 执行 → 结果回 history。**HA 生态中最接近 agentic loop 的方案。** GPT-4 级模型下多步执行可靠。

### 3.3 home-llm (本地微调)

**Repo:** https://github.com/acon96/home-llm | **Stars:** ~1.3k

- 对 Phi-3-mini / Llama-3.2-1B/3B 做 HA service-call 微调
- 输出结构化 `[HassCallService]` token 而非 OpenAI function JSON
- RPi5 + INT4 可跑 Phi-3-mini (3.8B)，单步可靠，多步有限

### 3.4 HA 自身方向

- Year of the Voice Chapter 1-11 (2023-2025): 重心在多语言 + 硬件，**有意不建 planner**
- 策略：提供 tool API，规划完全交给 LLM
- 2024.12: 意图匹配失败后 fall through 到 LLM agent

### Jarvis 启示

- **Extended OpenAI Conversation 的 YAML function-schema 注入** 方式直接可借鉴
- **home-llm 的微调路线** = 小模型 + 结构化输出训练，适合 RPi5
- HA 团队的判断：多步可靠性是 model 问题，不是 framework 问题

---

## 4. RPi + LLM 个人助手项目

### Tier 1 — 真正有设备控制

| 项目 | Stars | URL | 亮点 |
|---|---|---|---|
| **GPT-Home** | 637 | https://github.com/judahpaul16/gpt-home | **Philips Hue 原生集成** (同 Jarvis), LiteLLM + LangGraph + LangMem, Docker 部署 |
| **OpenClaw** | 100k+ | https://github.com/openclaw/openclaw | **跨设备 SSH 控制** (一等公民), MQTT, HA 集成, PicoClaw = RPi Zero 版 |
| **Max Headbox** | 334 | https://github.com/syxanash/maxheadbox | RPi5 上 100% 本地 (Ollama + Vosk), JS tool 模块架构干净 |
| **openLight** | 21 | https://github.com/evgenii-engineer/openLight | Go 单二进制, **确定性匹配优先 + LLM fallback**, systemd 服务控制 |

### Tier 2 — 语音 + LLM，少/无设备控制

| 项目 | Stars | URL | 亮点 |
|---|---|---|---|
| **be-more-agent** | 549 | https://github.com/brenpoly/be-more-agent | OpenWakeWord + Whisper.cpp + Piper + Ollama, 最干净的 RPi5 空白框架 |
| **pi-card** | 813 | https://github.com/nkasmanoff/pi-card | fine-tuned BERT (`tool-bert`) 做 tool dispatch, 新颖但停滞 |
| **OpenJarvis** (Stanford) | — | https://github.com/open-jarvis/OpenJarvis | 研究级, "Intelligence Per Watt", 关注效率指标 |

### 关键对比

| 特性 | GPT-Home | OpenClaw | Max Headbox | openLight |
|---|---|---|---|---|
| 跨设备 SSH | No | **Yes** | No | Partial |
| MQTT | Planned | **Yes** | Extensible | No |
| Philips Hue | **Yes** | Via HA | No | No |
| 语音 | Yes | Yes | Yes | No |
| 全本地 LLM | Optional | Yes | **Yes** | Yes |

### Jarvis 启示

- **OpenClaw** 的 SSH tool dispatch 架构最值得研究
- **GPT-Home** 是 Hue + LLM + voice 最近的精神兄弟，LangGraph tool-loop 值得看
- **openLight** 的确定性优先路由 (keyword match → LLM fallback) 可偷来优化 intent router 延迟

---

## 5. MCP (Model Context Protocol)

**Spec:** https://modelcontextprotocol.io/
**治理:** 2025-12 捐赠给 Linux Foundation (Agentic AI Foundation), OpenAI + Block 共同创立

### 核心设计

**是的，MCP 本质上就是在标准化 "atomic tools for LLMs"，但范围更广：**

| 原语 | 控制方 | 用途 |
|---|---|---|
| **Tools** | Model | 可执行函数 (读 DB, 调 API, 改文件) |
| **Resources** | Application | 只读数据 (文件内容, schema, 文档) |
| **Prompts** | User | 可复用交互模板 |

三方控制分离是有意的安全设计。

**协议:** JSON-RPC 2.0 (同 LSP), 有状态, capability negotiation。
**传输:** Stdio (本地) | Streamable HTTP (远程, 2025-03 替代 SSE, OAuth 2.1)。

### 生态规模

~10,000 公开 MCP server (2025-12 Anthropic 数据), 非官方目录 ~17,000。

**分类举例:** Filesystem, PostgreSQL/MySQL/SQLite/MongoDB, GitHub/GitLab, AWS/GCP/Azure, Slack/Discord, Linear/Jira, Sentry/Datadog, Playwright, Brave Search/Exa, **Home Assistant (官方)**

### 采用

| 客户端 | 状态 |
|---|---|
| Claude Desktop / Claude Code | 原生 |
| VS Code + GitHub Copilot | GA (2025-07) |
| Cursor, Windsurf, Continue | 内建 |
| Gemini CLI | 支持 |
| ChatGPT | Developer Mode 下支持 |
| LangChain / LangGraph / AutoGen | 社区适配器 |

### Home Assistant MCP (官方)

**HA 2025.2+ 内建:** https://home-assistant.io/integrations/mcp_server

- 在 `/api/mcp` 暴露 Streamable HTTP MCP server
- Claude Code 可直接连:
  ```bash
  claude mcp add-json "HA" '{"type":"http","url":"https://<ha>/api/mcp",...}'
  ```
- 支持 Tools + Prompts, 1.7% active HA 安装已使用
- 社区替代: `homeassistant-ai/ha-mcp` (2.3k stars)

### 成熟度

- **稳定:** 核心 Tools/Resources/Prompts, Stdio + HTTP 传输, OAuth 2.1
- **仍在演进:** 授权规范, 多 server 编排, 部分客户端远程支持不一致
- **安全:** 43% 测试的 MCP server 有命令注入缺陷 (Equixly/Backslash); tool poisoning 攻击已被演示 (Invariant Labs — 通过 "每日一事" server 偷 WhatsApp 消息)

### 对比 OpenAI Function Calling / LangChain Tools

| 维度 | MCP | Function Calling | LangChain Tools |
|---|---|---|---|
| 类型 | 开放协议 | 厂商 API 特性 | 框架抽象 |
| 工具发现 | 运行时 (`tools/list`) | 编译时 (每次 API 请求) | 应用启动 |
| 厂商锁定 | 无 | OpenAI only | 多 via adapter |
| 动态更新 | Yes | No (需 redeploy) | Limited |
| 最佳用途 | 多客户端/多 provider/生产共享 | 原型/小工具集 | 复杂编排/RAG |

**不是竞争关系，是不同层:** LangChain 做编排, MCP 做共享工具层, Function Calling 做临时/应用级工具。

### Jarvis 启示

1. **HA MCP 直接可用:** RPi5 上 HA 暴露 MCP server, Jarvis 可通过 HTTP 调用 HA tools — 比自建 MQTT bridge 标准化
2. **Jarvis 技能系统 → MCP server:** `skills.Skill` 架构与 MCP Tools 原语几乎同构，包一层即可让 Claude Code/Cursor 访问 Jarvis 技能
3. **安全需要注意:** 43% server 有注入缺陷，Jarvis 如果暴露 MCP 需要验证输入

---

## 总结：模式与启示

### 架构范式谱

```
约束程度:  松 ←────────────────────────────→ 紧

Open Interpreter     Computer Use       MCP/HA LLM API     home-llm
(任意代码执行)      (pixel-level 操作)    (typed tool schema)   (微调结构化输出)
```

- 越松 → 能力天花板高, 安全/可靠性差
- 越紧 → 可靠性高, 能力受 schema 限制
- **Jarvis 当前位置: MCP/HA 这一档** (typed skills + intent router)，这是对的

### 值得借鉴的

| 来源 | 可偷的设计 |
|---|---|
| Extended OpenAI Conversation | YAML function-schema 热注入, composite 链式调用 |
| OpenClaw | SSH tool dispatch 架构, 跨设备控制 |
| openLight | 确定性匹配优先 → LLM fallback (延迟优化) |
| home-llm | 小模型微调结构化 tool-call (RPi5 适用) |
| MCP | 将 Jarvis skills 包装为 MCP server, HA 控制走 MCP |
| Computer Use 三家 | atomic action + LLM planning loop 的通用模式确认 |

### Jarvis 不需要的

- Open Interpreter 式任意代码执行 — 安全模型不适合 always-on 家庭设备
- Computer Use 式 GUI 控制 — RPi5 是 headless
- 自建 HA 集成层 — 走 MCP 即可
