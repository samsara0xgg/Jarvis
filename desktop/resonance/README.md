# Jarvis Resonance prototype

Jarvis 的桌面语音界面（Electron + React + TypeScript）。桌面模式连接本机 daemon 的 Inherent v1 线：声纹跟随 daemon 的语音阶段，回复文字流式进入胶囊，文字输入、停止播报、麦克风静音、播报静音都发给 daemon。本进程不录音、不放语音，麦克风和扬声器归 daemon（见 `HANDOFF.md` 与 ADR-0015）。`npm run lab` 与验收脚本仍是本地模拟。

## 运行

常驻方式：仓库根目录 `jarvis daemon install` 会同时安装 daemon 和本界面两个 LaunchAgent，之后 `jarvis daemon restart` 让合并到 main 的代码生效。

手动方式：

```sh
cd desktop/resonance
npm ci
npm start
```

端口沿用 `JARVIS_INHERENT_BRIDGE_PORT`，默认 8006。

插件通过对话打开连接面板，也可从菜单「插件…」或外观设置进入列表。点击连接后才开始浏览器授权或使用输入的令牌；收起保留连接，取消停止当前连接。连接成功即时提供工具和技能；有待完成任务且对话未更新时自动继续。连接和操作审批分开，可在插件管理中调整审批或停用。

此功能需要同时更新后台和桌面应用。插件管理凭证由主进程从 `JARVIS_RUNTIME_ROOT`（默认 `~/.jarvis`）读取，启停和令牌另存于 runtime，不改写 YAML。`npm run build && node scripts/verify-plugins.mjs` 使用隔离的真实后台、MCP 和 OAuth 服务验证面板，截图与检查结果保存在 `evidence/plugins/`；不会登录个人账号。

- `npm run lab`：圆点与 Live 形变预览，含高清放大模型及保留的六状态圆环。
- `npm run package`：生成 `build/Jarvis Resonance.app`，用于本机打开；本地 ad-hoc 签名，未公证、未发布。
- `npm run verify`：构建并运行真实 Electron 窗口验收脚本；独立验收资料目录，系统音频输出静音。
- `node scripts/inspect-desktop.mjs --focus --silent`：Computer Use 辅助验收入口。临时允许窗口聚焦，输出窗口自身的几何信息到 `evidence/live-desktop.json`，不抓取其他应用。

Mac 原生材质编译需要 Xcode Command Line Tools 和 Node C headers；可用 `NODE_INCLUDE` 指定头文件目录。其他平台使用灰阶透明表面 fallback，未做平台验收。

## 当前交互

- 默认非 Live 胶囊为 120 × 40：发消息、圆点、Dashboard。点击圆点进入 Live，再点声纹收起，窗口保持可见。
- Live 中心外壳扩展为 174 × 40，发消息与 Dashboard 平滑移动到外侧，麦克风与播报静音逐渐出现；完整控制组为 270 × 40。图标固定 20，圆点与声纹同色。
- 圆点、声纹、外壳和两侧按钮共用连续动画进度；快速反向时保留位置和速度。只在进入／退出 Live 时改变外壳宽度。
- 非 Live 圆点跟随已有 UI 的待机、处理中、消息和失败状态；Live 声纹支持呼吸、聆听、思考、回应和静音。所有状态仍为本地模拟。
- 高清画布按实际显示倍率和设备像素比绘制。批准的圆点／声纹动画在减少动态效果开启时仍播放，隐藏窗口时暂停；通知折叠与面板入场也保留已确认的轻量过渡。
- 胶囊实体表面使用统一拖拽手势，包括按钮。移动超过 4 逻辑像素时视为拖动，释放不触发按钮；轻点保持按钮行为。透明间隙与圆角外区域穿透。
- 麦克风静音、播报声音静音、停止播报分别独立。点击声纹或在 Live 主界面按 Esc 会收回圆点并保持窗口可见；菜单隐藏操作保留当前模式，Cmd+. 仍可结束并隐藏。
- 文字入口在胶囊下方打开，保持顶部 Live 控件与模式。下方左侧附件按钮暂时禁用，右侧发送连接真实链路；仅 `--verify` 与 lab 的处理、回复为模拟。通知仅放结果、待回应、失败、用户设定提醒的预置示例。
- 文字输入、对话记录与 Dashboard 在同一 300 像素宽玻璃区域组合展示，默认依次排列；标题栏支持拖动排序（Alt + ↑/↓ 可键盘排序，Escape 取消拖动），独立箭头折叠与关闭，草稿在折叠或关闭后保留。超过屏幕可用高度时内部滚动，内容淡入淡出与连续高度变化分阶段衔接，快速反向保留运动状态。Dashboard 按钮统一打开／收起面板，通知卡片位于首页底部，最多显示两条，更多内容在区域内滚动，无通知时收起该区域；卡片保留回复、详情和清除操作。焦点在通知区域时 Esc 优先退出详情／回复，再关闭面板。
- 省略默认状态下常驻的 Listening 文案。辅助功能仍有状态描述，示例内容和菜单明确说明模拟性质。
- 悬停省略号、右键胶囊或菜单栏 J →「外观与提示音…」打开设置。不透明度与毛玻璃强度分别可调，设置保存在本原型的资料目录。强度控制原生毛玻璃的混合比例，不是任意半径的模糊调节。
- 默认遮罩不透明度 40%。图标按用户参考图描出 SVG 轮廓；还原证据与推断边界见 `design/fidelity-notes.md`。
- 已接入五类提示音。进入语音仍是来自录屏片段频带过滤的候选音，其现场噪声与操作时序存在不确定性，不声称与原版完全相同。
- 麦克风开/关与播报开/关改为实时合成的触感点击，移植 Hermes desktop 的 `selection` / `open` / `close` intent（引擎取自 web-haptics 0.0.6，MIT）：每个脉冲是一段 4 ms 噪声过 bandpass，中心频率与增益都随强度走，所以麦克风两个方向是同一声单击，播报开是渐强的一对、关是渐弱的一对。没有对应的音频资源。
- 默认低音量，可关闭或调节；不在启动、悬停、拖拽时播放，连续点击会平滑切断上一段。声音播放完毕会挂起 AudioContext。
- `Cmd+Shift+J` 隐藏/恢复，`Cmd+Shift+K` 文字输入，`Cmd+Shift+L` 键盘控制。菜单栏 `J` 提供恢复和退出。`Esc` 逐层收起，`Cmd+.` 结束语音。

额度页面提供「轻量标签 · 按类别」、「服务商切换 · 按服务商」和「B2 · 紧凑折叠」三种布局，在外观设置的「额度页面布局」中切换并自动保存。三版与 Dashboard、胶囊共用主题色、玻璃不透明度和原生毛玻璃强度；额度条使用主色的浅色变体，警戒色保留独立语义。额度重置时间显示相对倒计时，时间到期但尚未同步时显示「等待额度更新」。运行 `npm run build && node scripts/verify-quota-layouts.mjs` 可验证布局、设置联动与倒计时。

## 材质与边界

Codex 会话详情采用固定高度的紧凑滚动列表，无服务商图标和说明栏。普通行 32px，相邻收起行有淡分隔线；展开为 64px 两行胶囊时隐藏相邻分隔线。运行、待回应和未交互的完成项自动保持展开；点击底座或回复按钮后记为已交互，退出 Hover／回复后收起；新轮次或新的待处理状态重新展开，普通进度更新不会撑开。长按任务一秒固定，再次长按取消（键盘 P 等效），移动或滚动取消长按。左上小 × 从 Jarvis 移除本轮记录，可撤销；固定与移除跨窗口重开保留。普通记录保留最近 24 小时，活动中或固定的记录不因过期消失。点击任务底座打开对应 Codex 会话；回复按钮仅展开胶囊内的 Follow up 输入框，Esc 收回并保留本次面板内草稿。当前不支持直接向桌面 Codex 发消息或打断，输入框明确提示尚未接通发送，停止按钮标记不可用。同步失败会明确显示，不用演示数据代替。`node scripts/verify-codex-board.mjs` 用隔离 Electron 窗口验收交互，截图在 `evidence/codex-board/`。

`native/material.mm` 用公开 AppKit 的 `NSVisualEffectView` 将桌面毛玻璃裁切到各个控件。React/CSS 只绘制中性灰阶遮罩、边缘、图标；没有固定绿色底色。macOS 材质会受桌面和辅助功能设置影响；截图不保证与静态概念图像素一致。

普通悬浮显示不主动获得焦点；点击通知、文字输入和键盘入口会请求焦点，以接收 Esc 等键盘操作。拖拽期间允许跨屏，结束后按所在显示器工作区校正。支持范围以实际验收为准：已读取本机两个显示器（均为 2×，其中一个坐标原点为负）；完整 Spaces、全屏、不同 DPI 跨屏及屏幕热插拔体验仍须手工验收。不能把 API 设置当作已验证的系统行为。

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
- `evidence/desktop-orbit/verification.json`：当前圆点／Live 悬浮版验收结果；旧版验收文件保留作历史参考。
- `evidence/icon-fidelity.png`：参考轮廓与 Chromium 渲染结果。
- `evidence/notification-fade.json`、`notification-dismissal.json`：运行时逐帧透明度与尺寸。
- `design/analysis/feedback-source-map.json`：音效来源、截取时间及保留频带；`evidence/audio-signal-checks.json` 验证边界归零、峰值和无削波。

整体布局验收：`npm run build && node scripts/verify-dashboard-overview.mjs` 检查 Dashboard 开关、额度摘要、通知滚动／回复／空状态和 transcript 光缝。右键设置 →「体验三条通知」可加入三条预置示例。

组合区域验收：`npm run build && node scripts/verify-panel-stack.mjs` 使用隔离的 `--verify` Electron 实例，检查共存、排序、草稿、模拟发送与动画几何。

融合展开区当前采用中性黑色主题；此前的墨绿色文字、边线和悬停处理保存在 `src/surface-themes.ts` 的 `inkGreen` 预设，`activeSurfaceTheme` 选择当前配置，不另增设置界面。验收同时采样内容透明度、模块/容器高度和原生窗口尺寸；renderer 截图不代表原生桌面材质合成验收。
