# Jarvis Resonance prototype

Jarvis 的桌面应用（Electron + React + TypeScript）：住在刘海旁的 companion（入口 `electron/companion.ts`）。它连接本机 daemon 的 Inherent v1 线：回复文字流式进入界面，文字输入、停止播报、麦克风静音、播报静音都发给 daemon。本进程不录音、不放语音，麦克风和扬声器归 daemon（见 ADR-0015）。`--demo` 与验收脚本是本地模拟。

## 运行

常驻方式：仓库根目录 `jarvis daemon install` 会同时安装 daemon 和本界面两个 LaunchAgent，之后 `jarvis daemon restart` 让合并到 main 的代码生效。

手动方式：

```sh
cd desktop/resonance
npm ci
npm run companion
```

端口沿用 `JARVIS_INHERENT_BRIDGE_PORT`，默认 8006。

插件从 Dashboard 的插件格进入，在同一区域展开列表、连接和管理页面；对话唤起、菜单「插件…」和外观设置也进入此处。底部 bar 或 Esc 返回原来的网格位置，长内容在固定区域内滚动。点击连接后才开始浏览器授权或使用输入的令牌；收起保留连接，取消停止当前连接。连接成功即时提供工具和技能；有待完成任务且对话未更新时自动继续。连接和操作审批分开，可在插件管理中调整审批或停用。

此功能需要同时更新后台和桌面应用。插件管理凭证由主进程从 `JARVIS_RUNTIME_ROOT`（默认 `~/.jarvis`）读取，启停和令牌另存于 runtime，不改写 YAML。

- `npm run companion`：刘海小球界面。小球是星核（玻璃球里有星星，默认皮肤深空玻璃），住在刘海左侧的软件延伸里：停留且减速后探头、悬停出来并显示键盘入口、轻戳立即开始／打断／结束语音、对话区在她正下方（你说的和她说的按轮次显示，字幕分全部显示、只显示要看的、不显示三档，在设置 › 语音和她的右键菜单里选；打字的文字框也在里面）、点击刘海或停留且减速后打开 Dashboard；主页从刘海直接长出，她留在家中。她只有一个，跟着鼠标换屏：先缩回当前的岛，再从另一块屏的岛里冒出来；没有刘海的屏幕上她住在顶部正中间的黑色胶囊里，悬停胶囊两翼打开 Dashboard。皮肤另有星云、银河、磨砂、极光、图标同款（球里的星云取自 App 图标）：按住她，200 ms 出现蓄力环，650 ms 蓄满后松手，她原地跳起转一圈、闪一下，换成下一套；设置 › 她的样子 里可以直接挑皮肤，“自己换装”打开时她每隔 6 到 14 分钟会自己出来试一套、下次再换回你挑的那套，挑的皮肤重启后保留。表情用的是星核表情表里的 17 个加待机：聆听、思考、回应、文字回复完成在演示流程里会自动出现，设置 › 她的样子 ›“全部看一遍”依次演一遍。Dashboard 默认“围着她（一列）”：首页是她的话、Now、Agents（Claude 和 Codex 会话）、Usage 圆环，最后一行插件和项目两个小格；点一行原地长成详情页，面板高度不变，左上角“‹”或 Esc 返回，她按页面换表情；底部常显 Ask Jarvis… 文字入口，鼠标移上去或点击即可输入。默认连接本地 daemon；隔离测试用 `electron dist-electron/companion.js --demo`，此模式的转写、回复、批准和插件登录都是本地模拟，不连接 daemon、不录音、不播放语音。`npm run build && node scripts/verify-companion.mjs` 在无头 Chrome 里走完九个场景（第七个是换到没有刘海的屏幕，第八个是换皮肤，第九个是 Dashboard 各页），截图在 `evidence/companion/`。`node scripts/verify-talk-area.mjs` 用假 daemon 走对话区的各个状态（出现、字幕三档、跟读、写下来的部分、打字、收起、深想），截图在 `evidence/talk-area/`，加 `--frames` 再录下转场的每一帧。
第二轮可在设置 › 她的样子 › 刘海里的家切换原设计和精修；星核统一只自发光。暮色主页从标题向下拉过 78 pt 可撕成独立窗（⌘⇧↓），拖回刘海附近或 ⌘⇧↑ 放回，窗口里暂不增加家。

精修专项验收：构建后运行 `node scripts/verify-character.mjs`、`node scripts/verify-dashboard-refinement.mjs` 和 `node scripts/verify-notch-refinement.mjs`，分别检查角色生命层、主页数据与布局、刘海意图判定与审批操作；另用 `verify-character-material.mjs` 验证自发光与两种刘海，`verify-dusk-dashboard.mjs` 验证暮色像素边界与拖拽，`verify-dashboard-window.mjs` 验证独立 Electron 窗口，`verify-dashboard-handoff.mjs` 验证窗口间页面、草稿与操作入口的接续。异步历史与账户草稿用 `verify-dashboard-async-restore.mjs` 检查；审批草稿和接收水滴分别用 `verify-action-draft.mjs`、`verify-docking-drop.mjs` 检查；夜间挂机（ADR 0093）的睡前、夜里和昨晚卡片用 `verify-night-card.mjs` 检查。首页的早报卡片（两行以内、每天早上第一次打开才出现、重启不重现）和早报页（一屏扫完的分组短行、点开才看备注、超过五项折叠、定下来的事收进一个折叠）用 `verify-dashboard-brief.mjs` 检查。验收使用隔离的模拟数据。

- `npm run package`：生成 `build/Jarvis Resonance.app`，用于本机打开；本地 ad-hoc 签名，未公证、未发布。
- `npm run verify`：构建后依次运行 `scripts/` 下全部 `verify-*.mjs`（跳过会启动真实 Claude Code 的 `verify-agents-keeper`、`verify-agents-clear`），只打印失败脚本的输出。

Mac 原生材质编译需要 Xcode Command Line Tools 和 Node C headers；可用 `NODE_INCLUDE` 指定头文件目录。其他平台使用灰阶透明表面 fallback，未做平台验收。

## 提示音

- 已接入五类提示音。进入语音仍是来自录屏片段频带过滤的候选音，其现场噪声与操作时序存在不确定性，不声称与原版完全相同。
- 麦克风开/关与播报开/关改为实时合成的触感点击，移植 Hermes desktop 的 `selection` / `open` / `close` intent（引擎取自 web-haptics 0.0.6，MIT）：每个脉冲是一段 4 ms 噪声过 bandpass，中心频率与增益都随强度走，所以麦克风两个方向是同一声单击，播报开是渐强的一对、关是渐弱的一对。没有对应的音频资源。
- 默认低音量，可关闭或调节；不在启动、悬停、拖拽时播放，连续点击会平滑切断上一段。声音播放完毕会挂起 AudioContext。

额度页面提供「轻量标签 · 按类别」、「服务商切换 · 按服务商」和「B2 · 紧凑折叠」三种布局，在外观设置的「额度页面布局」中切换并自动保存。三版与 Dashboard、胶囊共用主题色、玻璃不透明度和原生毛玻璃强度；额度条使用主色的浅色变体，警戒色保留独立语义。额度重置时间显示相对倒计时，时间到期但尚未同步时显示「等待额度更新」。

## 材质与边界

Codex 会话详情采用固定高度的紧凑滚动列表，无服务商图标和说明栏。普通行 32px，相邻收起行有淡分隔线；展开为 64px 两行胶囊时隐藏相邻分隔线。运行、待回应和未交互的完成项自动保持展开；点击底座或回复按钮后记为已交互，退出 Hover／回复后收起；新轮次或新的待处理状态重新展开，普通进度更新不会撑开。长按任务一秒固定，再次长按取消（键盘 P 等效），移动或滚动取消长按。左上小 × 从 Jarvis 移除本轮记录，可撤销；固定与移除跨窗口重开保留。普通记录保留最近 24 小时，活动中或固定的记录不因过期消失。点击任务底座打开对应 Codex 会话；回复按钮仅展开胶囊内的 Follow up 输入框，Esc 收回并保留本次面板内草稿。当前不支持直接向桌面 Codex 发消息或打断，输入框明确提示尚未接通发送，停止按钮标记不可用。同步失败会明确显示，不用演示数据代替。

`native/material.mm` 用公开 AppKit 的 `NSVisualEffectView` 将桌面毛玻璃裁切到各个控件。React/CSS 只绘制中性灰阶遮罩、边缘、图标；没有固定绿色底色。macOS 材质会受桌面和辅助功能设置影响；截图不保证与静态概念图像素一致。

普通悬浮显示不主动获得焦点；点击通知、文字输入和键盘入口会请求焦点，以接收 Esc 等键盘操作。支持范围以实际验收为准：已读取本机两个显示器（均为 2×，其中一个坐标原点为负）；完整 Spaces、全屏、不同 DPI 跨屏及屏幕热插拔体验仍须手工验收。不能把 API 设置当作已验证的系统行为。

## 本地参考资料

以下录屏、分析图片与验收产物只保留在设计工作区，不提交到仓库；启动、构建和交互验收不依赖这些参考资料。验收脚本会重新生成 `evidence/`。

- `references/03-resonance.png`：最初选定的视觉概念。
- `design/material-study-v1.png`：用户确认的无色毛玻璃方向；静态生成稿。
- `references/codex-pet-interactions.mov`：最初交互录像。末段可观察到文字展开和声音斜杠；没有观察到点击中心 × 后的结果。
- `references/user-comparison.mov`：用户 2026-09-12 提供的同屏对比。首轮 Jarvis 约 730 × 116 视频像素，Codex 约 440 × 80；最初据此缩为约 220 × 40，随后按确认的 2.5:1 声纹区域加宽至 270 × 40 逻辑像素。拖拽规则另由用户明确说明。
- `evidence/`：验收输出。Playwright `capturePage` 图片只包含 renderer；Computer Use 原生截图单独标明，不混为一谈。

## 还原证据

- `design/analysis/feedback-source-map.json`：音效来源、截取时间及保留频带；`evidence/audio-signal-checks.json` 验证边界归零、峰值和无削波。
