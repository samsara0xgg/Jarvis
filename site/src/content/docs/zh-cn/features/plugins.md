---
title: 插件
description: Jarvis 通过 MCP 插件连接你的 Gmail、GitHub、Notion、日程和灯光；插件默认全关，她动手改东西之前会先问你。
---

Jarvis 不去重写你每天用的 App，而是通过 MCP 连上它们。MCP 是一套开放协议，让助手能调用别的服务提供的工具。每个插件在你打开之前都是关着的。

## 她能连上哪些账号？

仓库里自带四个插件，第五个连接靠配置接入。

- **Gmail**：搜索、阅读、起草和发送邮件。它在你的 Mac 上运行 Google 的 Workspace MCP server，只开了 Gmail 这一组功能。
- **GitHub**：通过 GitHub 官方托管的 server 处理仓库、issue、pull request 和 CI。
- **Notion**：通过 Notion 托管的 server 读写页面和数据库，另带四个技能：写规格、做调研、准备会议、沉淀知识。
- **Philips Hue**：开关、调亮度、切场景。它只和你家局域网里的 bridge 通信。
- **Microsoft To Do 和 Outlook 日历**：它们不在 `plugins/` 目录里，而是靠你自己在配置里加一个 Microsoft MCP server，之后会出现在同一个插件列表里；你自己加的其他 MCP server 也一样。

## 怎么打开一个插件？

要你自己来，没有任何东西会自己启动。出厂时一个插件都没有启用。如果你让她在一个还没连上的 App 里办事，她可以把插件面板打开，但登录要等你自己开始，后台进程从不会自己弹出浏览器。令牌和 key 存在只有你的账户能读的私有文件里，不会进对话。目前这些是明文文件，还没有加密。

<!-- shot: Dashboard 里的插件面板，Gmail 已连接、Notion 等待登录，只用示例数据 -->

## 她动手之前会做什么？

读东西她直接读，改东西之前先问你。每个工具按 server 自己声明的提示分类：只读的立刻执行；会删除、发送、合并，或者不能确定无害的，会变成一张确认卡，等你按按钮。只有 server 同时标明“不破坏”和“不碰外部世界”的写操作，才不用问。

你也可以按插件调整：总是问、只问写操作、或者从不问。Hue 是个好例子：查看灯的状态是预先批准的，开关一盏灯则要先问，除非你放宽。

<!-- shot: 对话里她最后一句话下方的确认卡，内容是一封回复 Gmail 的草稿，示例邮件 -->

## 她怎么找到该用的工具？

靠搜索。像 GitHub 这样的 server 会列出几十个工具，每次请求都全带上，既花钱又拖慢每一轮。所以插件的工具默认不在她的菜单上，要用时由一个叫 `tool_search` 的工具按名字、描述和参数去找。找到的工具只在这一轮有效；命中一个工具时，同一个 server 的只读工具也会一起带上，因为光搜不读往往没用。你问得最多的日程、待办和 Gmail 读取一直放在菜单上，这几类问题就省掉了搜索这一步。

## 你的数据去了哪里？

每个插件只和它自己的服务通信。她在对话里读到的内容，比如一封邮件，会和对话里的其他内容一样发给模型服务商。插件没有签名，也不声明自己需要什么权限，所以打开一个插件，就是选择信任它。

设计笔记：[工具排在搜索后面](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0034-plugin-tools-wait-behind-tool-search.md)、[按工具审批](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0033-mcp-tools-are-approved-per-codex-modes.md)。
