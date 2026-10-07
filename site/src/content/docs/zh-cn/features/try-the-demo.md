---
title: 试用演示
description: 一条命令，在内置示例数据上看到 Jarvis 的桌面伴侣，不需要 key、账号，也不需要后台进程。
---

在 Mac 上跑一条命令，就能看到 Jarvis 的界面在示例数据上运行。不需要 key，不需要账号，也不需要后台 daemon。

## 怎么运行？

把下面这行粘进“终端”：

```bash
curl -fsSL https://raw.githubusercontent.com/samsara0xgg/Jarvis/main/scripts/try-demo.sh | bash
```

第一次运行要几分钟，时间主要花在下载 Electron 上。脚本不长，写成了可以先读再跑的样子。它的全部内容都包在函数里，最后一行才执行 `main`，所以下载到一半被截断，也不会执行半截脚本。

<!-- shot: 终端里脚本的检查清单和“Continue?”提示，没有缺失项 -->

## 它先检查什么，装之前问什么？

先检查，改动任何东西之前只问一次。不是 macOS 就直接退出（Intel Mac 只给一个警告）。接着看有没有 Xcode Command Line Tools（伴侣里那块原生玻璃模块要用它编译），以及 Node.js 24 或更新版本。然后列出打算安装的东西，等你输入 `y`；回答否，就以“Nothing was installed.”结束。

- **Command Line Tools**：打开苹果自己的安装对话框。
- **Node.js 24**：从 nodejs.org 下载官方安装包，对照官方公布的校验值检查，macOS 会要你输入开机密码。
- **Jarvis**：浅克隆到 `~/Jarvis`。已有的 Jarvis 克隆会更新；不是 Jarvis 的文件夹不会动。
- **npm 依赖**：装在这个文件夹里，然后启动演示。

它从不索要 key，也不会调用任何付费 API。想放到别处，加 `--dir <路径>`。

## 演示里能看到什么，看不到什么？

能看到：刘海旁边的伴侣、她的 agent 小星星，以及 Dashboard，里面填着示例的日程、待办、邮件、早报和 agent。戳一下小球，她会演一段预先写好的对话。没有刘海的屏幕上，她住在顶部的小黑胶囊里。

看不到：她不会听，也不会说，里面没有任何一条是你自己的数据。回复、批准和插件登录全是本地模拟，完整的 Agents 窗口（Startrail）也不会打开。这些要完整安装之后才有。

<!-- shot: 一段短循环：刘海旁的小球展开 Dashboard，里面是示例邮件和待办 -->

## 怎么退出？

在运行它的终端窗口里按 Ctrl+C。

## 想自己动手装？

已经装好 Xcode Command Line Tools 和 Node.js 24 的话：

```bash
git clone https://github.com/samsara0xgg/Jarvis
cd Jarvis/desktop/resonance
npm ci
npm run companion -- --demo
```

## 真正跑起来

目前 Jarvis 是开发者的装法，不是安装包。它为一个人、一台 Mac 而做，从源码目录运行，还不是签名的 App，有些默认值仍假设是作者自己的机器。真跑起来还需要 Python 3.12 和 uv、负责麦克风和扬声器的 daemon、一个 OpenAI key，以及一次性下载的语音模型。面向其他人的公开版本放在以后。具体步骤见 README 的“Run it for real”一节。
