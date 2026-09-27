# Jarvis 发布体验设计

18 张独立 PNG，覆盖图标、菜单栏、安装、首次启动、错误恢复、断连、关于与命名。全部由内置 ImageGen 分别生成；每张只展示一个完整场景，没有拼图。

[打开逐张画廊](index.html) · [完整设计方向](DIRECTION.md) · [原始图片](images/) · [完整提示词](prompts/)

**这是设计交付，不是已实现功能。** 本轮没有修改应用代码。按钮、状态切换、下载数字和恢复流程用于明确目标体验；真实接线要求见下文。

设计延续现有星核：深色光学玻璃球、蓝紫内部星光、两只向内倾斜的象牙白眼睛。背景与界面保持克制，光主要来自小球。参考包括当前 Electron/React 与后台代码、现有面板和首次引导截图，以及在浏览器中查看的开场演示；没有声称观看过本地实录视频。

## 1. App 图标

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 01 · 应用主图标 | [01-app-icon.png](images/01-app-icon.png) | [01-app-icon.txt](prompts/01-app-icon.txt) |

让 Finder、应用程序文件夹、系统权限和通知入口共用同一个 Jarvis。该 PNG 是视觉母稿，发布前还需制作并检查 macOS 多尺寸 `.icns`，配置 Bundle 图标，验证系统实际呈现。

## 2. 菜单栏图标

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 02 · 单色透明图形概念 | [02-menu-template.png](images/02-menu-template.png) | [02-menu-template.txt](prompts/02-menu-template.txt) |
| 03 · 浅色菜单栏 | [03-menu-light.png](images/03-menu-light.png) | [03-menu-light.txt](prompts/03-menu-light.txt) |
| 04 · 深色菜单栏 | [04-menu-dark.png](images/04-menu-dark.png) | [04-menu-dark.txt](prompts/04-menu-dark.txt) |

圆形轮廓与镂空双眼保留辨识度。02 是 ImageGen 生成的 alpha 概念稿，发布前必须整理成严格单色的矢量／Template 资源，检查小尺寸像素，并交由 macOS 自动着色。03、04 是效果示意，不是两套需要手动切换的图标。

菜单统一使用“Jarvis”“打开面板”“隐藏小球”“设置…”“关于 Jarvis”。当前代码已经采用隐藏／显示，不再是“退出小球”；隐藏后仍保留后台与听写，不应与真正退出混淆。

## 3. 安装包窗口

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 05 · 拖入应用程序 | [05-installer.png](images/05-installer.png) | [05-installer.txt](prompts/05-installer.txt) |

一条明确的拖动路径，让首次安装无需阅读说明。此图是完整窗口构图，**不是可直接铺入 DMG 的最终背景**：落地时需另导出不含系统图标、文字标签与窗口边框的背景，并设置 Finder 窗口大小、图标位置及 Applications 链接。

## 4. 启动与语音准备

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 06 · 正在醒来 | [06-waking.png](images/06-waking.png) | [06-waking.txt](prompts/06-waking.txt) |
| 07 · 正在准备声音 | [07-voice-download.png](images/07-voice-download.png) | [07-voice-download.txt](prompts/07-voice-download.txt) |
| 08 · 声音马上就绪 | [08-voice-activating.png](images/08-voice-activating.png) | [08-voice-activating.txt](prompts/08-voice-activating.txt) |

启动先给出可见回应，再进入原有开场。模型下载允许继续设置、先用文字；下载后的重启独立显示为激活阶段，避免让用户把短暂不可用误认为故障。

07 的“148 MB / 240 MB”“62%”是同一时刻的**示例数据**。代码中 SenseVoice int8 约 239 MB，另有词表和 VAD；“约 240 MB”用于说明首次下载规模。当前没有下载进度接口，不能据此宣称已能显示真实百分比、断点续传或精确剩余时间。

## 5. 对话错误

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 09 · 密钥被拒 | [09-error-key.png](images/09-error-key.png) | [09-error-key.txt](prompts/09-error-key.txt) |
| 10 · 模型权限 | [10-error-model.png](images/10-error-model.png) | [10-error-model.txt](prompts/10-error-model.txt) |
| 11 · 账户额度 | [11-error-quota.png](images/11-error-quota.png) | [11-error-quota.txt](prompts/11-error-quota.txt) |
| 12 · 模型服务网络 | [12-error-network.png](images/12-error-network.png) | [12-error-network.txt](prompts/12-error-network.txt) |
| 13 · 请求超时 | [13-error-timeout.png](images/13-error-timeout.png) | [13-error-timeout.txt](prompts/13-error-timeout.txt) |
| 14 · 请求限流 | [14-error-rate.png](images/14-error-rate.png) | [14-error-rate.txt](prompts/14-error-rate.txt) |

六张保持同一个对话外壳：保留用户原话和正常输入区，用局部错误卡解释本次失败。密钥与权限用珊瑚色，临时受阻用暖琥珀色；这不等于 Jarvis 后台离线。

| 后端原因 | 标题 | 正文 | 主操作 / 次操作 | 图片 |
| --- | --- | --- | --- | --- |
| `missing_key` | 还差一把密钥 | 添加模型服务的 API 密钥，就能开始对话。 | 添加密钥 / 稍后 | 文案规范 |
| `unauthorized` | 这把密钥用不了了 | 模型服务拒绝了当前密钥。检查是否完整，或换一把新的。 | 更换密钥 / 查看详情 | 09 |
| `model_denied` | 暂时用不了这个模型 | 当前密钥没有该模型的访问权限。开启权限后，可以再试一次。 | 查看模型权限 / 更换密钥 | 10 |
| `quota` | 模型账户额度不足 | 这次对话没有完成。补充额度后可以继续。 | 查看账户额度 / 稍后重试 | 11 |
| `network` | 暂时连不上模型服务 | 检查网络连接后，再试一次。 | 再试一次 / 查看详情 | 12 |
| `timeout` | 这次等得有点久 | 模型服务没有及时回应。可以重新发送这条消息。 | 再试一次 / 关闭 | 13 |
| `rate_limited` | 稍等一下，再继续 | 模型服务暂时限制了请求频率。稍等片刻后，可以再试一次。 | 再试一次 / 关闭 | 14 |
| `error` | 这次没能完成 | 遇到了一个问题，可以再试一次。 | 再试一次 / 查看详情 | 文案规范 |

后端已有以上八种原因，并发送 `reason` 与本地化 `message`；当前前端丢弃它们，仍使用固定报错。09、10、14 的状态码是示例，必须以真实错误为准；`model_denied` 也包含模型不存在，落地文案应按详情解释，不能一律认定是 403。

“更换密钥”、权限与额度页面入口、错误详情和显式重试还需接线。首次引导已有测试并保存密钥的接口，普通设置账户页目前仅显示状态。重试必须由用户触发；“稍后重试”不创建自动任务。

## 6. 后台断连与恢复

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 15 · 断连并保留草稿 | [15-offline.png](images/15-offline.png) | [初稿](prompts/15-offline.txt) · [最终小球修订](prompts/15-offline-revision.txt) |
| 16 · 正在重新连接 | [16-reconnecting.png](images/16-reconnecting.png) | [16-reconnecting.txt](prompts/16-reconnecting.txt) |

小球在断连时降低亮度，并附小型断链标记；面板明确显示不可发送，草稿留在原位。重连时恢复少量光线，以低频呼吸提示正在处理，避免反复弹窗。

当前 WebSocket 已有自动重连，Dashboard 也有“离线 · 重连中”；图中的草稿持久化保证、完整状态页、诊断与恢复按钮属于新设计。后台不可达时，恢复不能依赖该后台自身的 HTTP 重启接口，需由 Electron 管理启动。不能在重连后自动补发旧消息。

## 7. 关于与署名

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 17 · 关于 Jarvis | [17-about.png](images/17-about.png) | [17-about.txt](prompts/17-about.txt) |

版本以当前代码的 **0.1.0** 为准，落地时读取构建元数据。关于页同时提供“开源组件与许可证”和“复制诊断信息”的拟议入口。**Jarvis 自身许可证尚未确认，不把 Jarvis 标为 MIT、Apache 或开源。**

署名保留 **SenseVoiceSmall · FunASR / FunAudioLLM**，注明 **FunASR Model Open Source License v1.1**，以及 **ONNX 转换 · sherpa-onnx / csukuangfj**。模型许可证 §2.2 要求注明出处、作者并保留模型名称；并未要求必须放在“关于”页，本设计选择此处使用户容易找到。

- [SenseVoiceSmall 官方模型卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall)
- [FunASR 模型许可证原文，§2.2](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)
- [当前使用的 sherpa-onnx ONNX 模型来源](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17)
- [仓库第三方声明快照](references/THIRD-PARTY-NOTICES.md)：复制自 `desktop/resonance/THIRD-PARTY-NOTICES.md`。继续保留 Silero VAD、hey_jarvis、推理运行时、Electron、Chromium 与 CPython 的相应声明和完整许可证，不用关于页摘要替代它们。

## 8. 对外名称

| 交付 | 原图 | 提示词 |
| --- | --- | --- |
| 18 · 品牌与部件命名 | [18-naming.png](images/18-naming.png) | [18-naming.txt](prompts/18-naming.txt) |

| 对象 | 统一叫法 |
| --- | --- |
| 产品、安装包、进程与系统权限主体 | **Jarvis** |
| 桌面交互部件 | **小球 / Orb** |
| 已有角色与视觉身份 | 保留**星核**，不另造吉祥物 |
| 用户给助手起的名字 | 个人称呼，例如 Nova；不改产品名 |
| 产品短句 | **住在你 Mac 上的助手** |

## 状态与动效约束

- **醒来**：启动即出现轻量可见反馈；微弱呼吸和睁眼，使用不定进度，不显示虚假百分比或倒计时。
- **下载**：进度只由真实已下载字节驱动；不知道总量时改为不定进度。网络停顿不伪造增长，准备期间可继续文字与设置。
- **激活**：下载完成与服务可用分开判断；重连期间禁用发送和语音，保留输入，不把模型下载完成称为全部就绪。
- **错误**：局部柔和出现，不闪屏、不播放惩罚性音效；一次清楚解释加一个主要恢复动作。
- **断连**：保留上下文与草稿，显示可恢复状态；重连成功后恢复控件，旧请求不自动重发。
- **减少动态效果**：停止绕圈与空间位移，保留静态状态标记和必要的文字变化；动效不能成为理解状态的唯一途径。

## 交付边界

图片用于视觉与交互方向评审，提示词用于复现和继续迭代；它们不是可执行组件、最终切图或已验证的恢复实现。生产落地仍需真实图标资源、DMG 分层背景、状态契约、错误动作接线与 macOS 实机验收。
