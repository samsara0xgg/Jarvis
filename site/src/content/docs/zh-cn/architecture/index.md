---
title: "它是怎么搭起来的"
description: "一个 Python 守护进程管麦克风、扬声器、模型、记忆和工具，Electron 伴侣应用管屏幕。守护进程的代码分成六层，每个设计决定都留有一份记录。"
---

Jarvis 是同一台 Mac 上两个只和对方说话的程序，外加一份分了层、层与层之间不许越界的代码。这一页是地图；[设计笔记](design-notes/)把其中四个决定写成了小故事。

## 两个进程

**守护进程**是 Python 3.12。麦克风、扬声器、本地语音模型、记忆、工具，以及每一个发给语言模型的请求都归它管，机器上别的东西碰不到这些。

**伴侣应用**用 Electron、React 19 和 TypeScript，再加一个很小的 AppKit 模块（Objective-C++）来画那团玻璃。你看得见的东西归它：刘海旁的小球、Dashboard、首次设置和设置页。它不带模型，也没有记忆。打开 Agents 窗口时，旁边还会起一个小的 Node 宿主，让你的 Claude Code 和 Codex 会话在重启之后还能接着跑。

两边通过 localhost 通信，用一把每台机器各自的密钥。守护进程只接受发给 `127.0.0.1` 或 `localhost` 的请求（所以把自己域名重绑到你机器上的网页也进不来），除了存活探测，每个接口都要带运行目录里的那把密钥。

## 语音这条线

唤醒词用 microWakeWord（“Hey Jarvis”）。语音识别用 SenseVoice，配 Silero 做语音活动检测，较长的话可以交给本地 Whisper。她的声音来自 MiniMax，通过 WebSocket 流式返回。唤醒词、VAD 和识别都在 Mac 本机运行；首次启动会下载约 240 MB 的模型，下完之前守护进程只跑文字。存储全程是 SQLite。

## 六层

守护进程的代码分成六个包，按“允许知道什么”来编号。

| 层 | 管什么 | 示例模块 |
|---|---|---|
| L1 宪法 | 产品身份、原则和非目标，以冻结的常量存在。什么都不 import。 | `jarvis/constitution/__init__.py` |
| L2 状态 | 只追加的事件日志、从中折出来的投影、`memory.db` 和其他本地存储 | `jarvis/state/event_log.py` |
| L3 决策 | Tier 0 路由、策略和闸门、每一轮发给模型的请求怎么拼、日报和工作状态的分析 | `jarvis/decision/gates.py` |
| L4 执行 | 工具、worker、插件和 MCP 客户端、浏览器护栏 | `jarvis/execution/mcp_tools.py` |
| L5 表面 | 语音、HTTP 和 WebSocket 接口、Claude Code 和 Codex 会话、屏幕和用量的观察者 | `jarvis/surface/voice_pipeline.py` |
| L6 部署 | 运行目录、launchd、睡眠唤醒、模型下载、导出和抹掉 | `jarvis/deployment/launchd.py` |

六层之外还有三个不算层的包：`jarvis/runtime/`（接线，`daemon.py`）、`jarvis/cli/`（入口）和 `jarvis/shared/`（跨层的参考数据，比如 `pricing.py`，哪里都能 import）。

### 只有 `runtime/` 能跨层接线

import 只能单向走。`cli` 可以 import `runtime`；`runtime` 可以 import 中间四层；中间四层（决策、执行、表面、部署）可以 import `state`；`state` 可以 import `constitution` 和 `shared`；这两个什么都不 import。中间四层彼此是平级的，不能互相 import：语音代码够不着工具，决策代码也够不着语音。

这条规矩由构建来执行。`lint-imports` 读 `.importlinter` 里的约定，哪个包违反就报错；它在仓库的初始化脚本里跑，也是每次提交前的一道门。两层必须配合时，由 `runtime/` 把一层的函数交给另一层。比如决策快照的缓存放在 `jarvis/runtime/decision_state.py`，以一个“读取函数”的形式交给决策层，决策层因此不需要 import runtime 里的任何东西。

## 状态脊柱

Jarvis 知道的一切都朝一个方向流：**事件日志，再到投影，再到记忆**。

- **事件日志**是一张只会变长的 SQLite 表。触发器拒绝一切 `UPDATE` 和 `DELETE`，更正只能追加新事件。每条事件记的是谁在什么时候报告了什么，事件类型登记在同一个文件里，截图、录音这类大块内容存成文件，日志里只放引用。
- **投影**是日志折出来的结果：待确认的卡片、没结束的动作、最近几轮。模型、工具和界面都不能写它。每一轮读的是折到最新事件的结果；从 ADR 0164 起，它只折上次读过之后新增的事件，结果和从头折一遍完全一样。
- **记忆**是 `memory.db`：逐字的对话、每天的摘要，以及夜里改写的那份关于你的、带版本的笔记。它之上的东西都是派生出来的，可以重建。

## 决定怎么留下来

每个挪动边界或合同的决定，都在 [`docs/adr`](https://github.com/samsara0xgg/Jarvis/tree/main/docs/adr) 里留一份 ADR：到目前为止 177 份，一个决定一个文件，一份一页。规范（`docs/adr/README.md`）要求必须有 **Alternatives rejected**（被否决的方案）一节，而且落选的理由要能用一个数字或一次可复现的观察来反驳，因为只有这一部分是代码和规格里都不会写的。`scripts/check_adrs.py` 检查格式、状态取值、200 行上限和悬空引用。要改一个已经接受的决定，就写一份新的 ADR，把旧的标为被取代，不去改旧的。

## 完整规格

一个公开版本必须满足什么，写在 [`docs/spec.html`](https://github.com/samsara0xgg/Jarvis/blob/main/docs/spec.html) 里：进程、状态脊柱、一轮怎么执行、哪些东西会离开这台 Mac、威胁和对应的护栏、密钥、生命周期。规格是中文写的；ADR 是英文写的。
