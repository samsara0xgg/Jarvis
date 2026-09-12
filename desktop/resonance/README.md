# Jarvis Resonance prototype

独立 Electron + React + TypeScript 桌面交互原型。所有输入、语音、回复、任务结果均为本地模拟，不连接 Jarvis 核心，不录音，不保存对话，不派工。

## 运行

```sh
cd desktop/resonance
npm ci
npm start
```

- `npm run lab`：独立开发预览，可切换模拟阶段、背景和示例通知。
- `npm run package`：生成 `build/Jarvis Resonance.app`，用于本机打开；本地 ad-hoc 签名，未公证、未发布。
- `npm run verify`：构建并运行真实 Electron 窗口验收脚本；独立验收资料目录，系统音频输出静音。
- `node scripts/inspect-desktop.mjs --focus --silent`：Computer Use 辅助验收入口。临时允许窗口聚焦，输出窗口自身的几何信息到 `evidence/live-desktop.json`，不抓取其他应用。

Mac 原生材质编译需要 Xcode Command Line Tools 和 Node C headers；可用 `NODE_INCLUDE` 指定头文件目录。其他平台使用灰阶透明表面 fallback，未做平台验收。

## 当前交互

- 声纹区域为 90 × 36（2.5:1），只增加中间宽度；两侧图标仍是 20，按钮与整体高度仍是 40。
- 默认控制组约 270 × 40 逻辑像素。左右独立按钮分别为文字和通知，中间为麦克风、细丝声纹、播报声音。
- 声纹支持呼吸、聆听、思考、回应、静音五种姿态，通过阻尼平滑收紧与释放；悬停或键盘聚焦时淡出并显示结束按钮。右键设置 →「体验完整动效」播放一轮模拟节奏，也可单独选状态、调整主题色。默认安静呼吸，不把模拟运动当作真实音频输入。
- 系统减少动态效果时保留静态声纹，页面隐藏时暂停绘制；主题色不影响无色玻璃。`node scripts/verify-presence.mjs` 验收实际 Canvas 输出及演示流程。
- 胶囊实体表面使用统一拖拽手势，包括按钮。移动超过 4 逻辑像素时视为拖动，释放不触发按钮；轻点保持按钮行为。透明间隙与圆角外区域穿透。
- 麦克风静音、播报声音静音、停止播报分别独立。结束语音隐藏胶囊并回到待机，保留示例任务；单纯隐藏保留当前模式。
- 文字入口横向展开。示例输入的处理与回复是计时器模拟，不建立真实任务。通知仅放结果、待回应、失败、用户设定提醒的预置示例。
- 展开后的控制组宽 330 像素，以原中心快速对称展开；通知距控制组 12 像素，支持 180 ms 淡出和单条移除收拢，毛玻璃同步变化。
- 省略默认状态下常驻的 Listening 文案。辅助功能仍有状态描述，示例内容和菜单明确说明模拟性质。
- 悬停省略号、右键胶囊或菜单栏 J →「外观与提示音…」打开设置。不透明度与毛玻璃强度分别可调，设置保存在本原型的资料目录。强度控制原生毛玻璃的混合比例，不是任意半径的模糊调节。
- 默认遮罩不透明度 40%。图标按用户参考图描出 SVG 轮廓；还原证据与推断边界见 `design/fidelity-notes.md`。
- 已接入进入语音、麦克风开/关、播报开/关五类候选提示音，来自录屏片段的频带过滤。默认低音量，可关闭或调节；不在启动、悬停、拖拽时播放，连续点击会平滑切断上一段。声音播放完毕会挂起 AudioContext。录屏的现场噪声和操作时序存在不确定性，尚未核实听感，不声称与原版完全相同。
- `Cmd+Shift+J` 隐藏/恢复，`Cmd+Shift+K` 文字输入，`Cmd+Shift+L` 键盘控制。菜单栏 `J` 提供恢复和退出。`Esc` 逐层收起，`Cmd+.` 结束语音。

## 材质与边界

`native/material.mm` 用公开 AppKit 的 `NSVisualEffectView` 将桌面毛玻璃裁切到各个控件。React/CSS 只绘制中性灰阶遮罩、边缘、图标；没有固定绿色底色。macOS 材质会受桌面和辅助功能设置影响；截图不保证与静态概念图像素一致。

普通显示与通知展开不主动获得焦点，文字输入和键盘入口才请求焦点。拖拽期间允许跨屏，结束后按所在显示器工作区校正。支持范围以实际验收为准：已读取本机两个显示器（均为 2×，其中一个坐标原点为负）；完整 Spaces、全屏、不同 DPI 跨屏及屏幕热插拔体验仍须手工验收。不能把 API 设置当作已验证的系统行为。

## 本地参考资料

以下录屏、分析图片与验收产物只保留在设计工作区，不提交到仓库；启动、构建和交互验收不依赖这些参考资料。验收脚本会重新生成 `evidence/`。

- `references/03-resonance.png`：最初选定的视觉概念。
- `design/material-study-v1.png`：用户确认的无色毛玻璃方向；静态生成稿。
- `references/codex-pet-interactions.mov`：最初交互录像。末段可观察到文字展开和声音斜杠；没有观察到点击中心 × 后的结果。
- `references/user-comparison.mov`：用户 2026-09-12 提供的同屏对比。首轮 Jarvis 约 730 × 116 视频像素，Codex 约 440 × 80；最初据此缩为约 220 × 40，随后按确认的 2.5:1 声纹区域加宽至 270 × 40 逻辑像素。拖拽规则另由用户明确说明。
- `evidence/`：验收输出。Playwright `capturePage` 图片只包含 renderer；Computer Use 原生截图单独标明，不混为一谈。

本目录是独立设计迭代原型，语音接线入口与边界见 `HANDOFF.md`。

## 还原证据

- `design/fidelity-notes.md`：逐项说明已验证行为、推断和剩余限制。
- `evidence/verification.json`：最近一次真实 Electron 验收结果。
- `evidence/icon-fidelity.png`：参考轮廓与 Chromium 渲染结果。
- `evidence/notification-fade.json`、`notification-dismissal.json`：运行时逐帧透明度与尺寸。
- `design/analysis/feedback-source-map.json`：音效来源、截取时间及保留频带；`evidence/audio-signal-checks.json` 验证边界归零、峰值和无削波。

通知细节 v2：多条通知初始重叠，点击最前面的卡片才展开；hover 不展开。展开后可在卡片内回复并看到明确的模拟回执。右键设置 →「体验通知叠层」添加两条预置示例。`node scripts/verify-notification-details.mjs` 检查叠层、点击展开、键盘操作、回复与 hover。
