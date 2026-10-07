---
title: 在刘海里管 Claude Code 和 Codex
description: 你的编程 agent 会变成刘海旁的星星，需要你拿主意时，一张卡片从刘海垂下来，不用切窗口就能回答。
---

Jarvis 替你盯着 Claude Code 和 Codex 的会话，你不用再一个个翻终端。每个会话都会出现在刘海旁边，哪个需要你，问题就送到你面前。

<!-- shot: 刘海右侧一排小星星（一颗“等你”，两颗“在干活”，各带数字），只用示例会话 -->

## 一眼看清所有会话

刘海旁的那一排按“要你做什么”分组：轮到你、在干活、做完了、先放着，每组带一个数字。在干活的星星缓缓转动，在等你的星星会一圈圈发光。

鼠标停在这一排上，刘海会向下展开成一张列表，每个会话一行：名字，加上它此刻在做什么。可以在这里看它最近说了什么，把它先放着，或者做完后归档。如果你正盯着某个会话所在的终端，她就不再为它弹卡片。

<!-- shot: 展开的刘海面板，“轮到你”和“在干活”两组下各有几行会话，项目名用示例 -->

## 在你手头的地方批准权限请求

Claude Code 想跑一条命令或改一个文件时，刘海会垂下一张卡片，写着命令和它运行的目录，你在这里点允许或拒绝。Claude 提出的问题和计划也走同一张卡：选一个选项，批准计划，或者说清楚要改哪里。

<!-- shot: 刘海垂下的卡片，里面是一条 shell 命令，带“拒绝”“允许”两个按钮，示例命令 `npm run build` -->

她的做法是扣住 Claude Code 自己的权限 hook，等你回答再放行，并不替换它原来的提示。终端里的对话框照样开着，你先答哪边，哪边算数。如果没人看得到卡片，比如伴侣没开，或者你开了安静档，她就放手，让 Claude Code 照老样子自己问，所以不会有 agent 卡在一张没人看见的卡片上。

Codex 是只看不答。Codex 的审批会出现一张“等你”的卡片，带你跳去 Codex 里处理，因为 Jarvis 只是听着 Codex，从不替它做决定。

## 把“做完了”里藏着的提问挑出来

agent 一轮结束时问一句“这两个方案你要哪个？”，从远处看就像它只是做完了。可以打开一个可选的设置，让一个小的云端模型读最后一条消息的结尾，只有它很有把握时，才把这条从“做完了”升级成“等你”。它只会升级、不会把任何一条压下去；默认关闭，因为这段文字会离开你的 Mac。

## Startrail，Agents 窗口

Startrail 是更完整的窗口，用来并排跑 Claude Code 和 Codex 的会话。它的列表按等得最久的排在前面，新会话默认开自己的 git worktree，还能把会话的改动落到仓库里。它在最前面时，刘海保持安静，同一件事不会提醒你两遍；它被别的窗口盖住时，它的会话会加入刘海旁的星星，用同样的卡片。

它还在做。Startrail 目前只能从源码构建打开，并且要求这台 Mac 上有你自己的 Claude Code 和 Codex 登录。演示版和安装版都打不开它：把 Claude Code 装进 App 之前，得先把 Anthropic 的条款理清楚。现在能试的是 Dashboard 里的 Agents 页，演示版里有几个示例会话，可以批准，也可以拒绝。

<!-- shot: Dashboard 的 Agents 页，“等你”和“在干活”两组，示例会话，能看到批准、拒绝按钮 -->

读取 Claude Code 会话列表默认是关的；给卡片供料的两个小 hook 脚本目前要手动拷进去，还没有安装器。

Design notes: [Jarvis holds Claude Code permission prompts](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0049-jarvis-holds-claude-code-permission-prompts-for-the-notice-card.md), [Jarvis's notch says what Startrail would notify](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0104-jarviss-notch-says-what-startrail-would-notify.md)
