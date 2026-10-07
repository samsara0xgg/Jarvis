---
title: 用量、额度与花费
description: 一个页面看清 Claude 和 Codex 的套餐用了多少、各个额度什么时候重置，以及 token 按天、按会话花在了哪里。
---

用量页把两件事放在一处：Claude 和 Codex 的套餐用掉了多少，什么时候恢复。往下还能看到 token 都花去了哪里。

<!-- shot: 用量页整页：余额一行、Claude 和 Codex 的圆环加重置倒计时、OpenAI 花费圆环；只用示例百分比 -->

## 一眼看到每个套餐还剩多少

每个订阅的每个额度窗口都有一个圆环。Claude 有 5 小时窗口和每周窗口，套餐如果还有针对某个模型的周上限，会多一个单独的环；Codex 显示它自己的窗口。每个环下面是距离重置的倒计时。环越满颜色越暖，快到顶时会变成醒目的警示色，免得额度在你干活干到一半时突然把你拦住。Dashboard 首页上还有一个缩小的版本。

这些数字取自这台 Mac 上已经登录的 Claude Code 和 Codex，Jarvis 大约每五分钟问一次，只记百分比、金额、时间和套餐名，不记任何凭据。套餐额度这部分默认关闭，原因也在这里：它要读另一个程序的登录状态。

数字来自服务商没有公开文档的接口。接口一变，那一行会直接告诉你哪里出了问题，不会摆出一个错的数字。这一页会不会放进公开版，目前还没有定。

## 有意识地花掉一次 Codex 重置

Codex 有时会发放额度重置；Claude 这边页面会显示还剩几次、用到哪天为止。Codex 的重置可以直接在页面里花掉，但要经过一次确认，确认按钮晚一会儿才生效，防止手滑双击。每次确认只会花一次，哪怕你重试。

<!-- shot: Codex 圆环下方的“用掉这次重置？”确认框，文字“用掉 2 次中的 1 次”，示例数据 -->

## 看 token 花去了哪里

Token 一栏读取 Claude Code 和 Codex 在本机留下的日志，展示最近 30 天：每天一根柱子，每个 agent 占多少，再加一份按花费排序的会话列表。可以切换今天、7 天、30 天，点开一个会话能看到它用了哪些模型。

所有金额都按 API 价格折算，把它当作衡量工作量的尺子，而不是订阅账单。计算在你的 Mac 上完成，用的是本来就在那里的文件。

<!-- shot: Token 一栏，7 天的堆叠柱状图、各 agent 占比和三个会话，其中一个展开显示模型；会话标题用示例 -->

配了管理密钥的话，OpenAI API 的花费会以按模型切分的圆环显示今天的数字，并给出本月累计。OpenAI 不提供余额，所以由你填一次账单页上的数字，Jarvis 再减去之后的花费，显示的是估算值。

## 一天花得偏多时，提醒你一下

某一天记下的模型花费第一次超过上限，刘海会垂下一张卡片告诉你。默认上限是每天一美元，可以改。

它不会停掉、拖慢或切换任何模型。对话说到一半她突然没声音，比多花几分钱糟糕得多。这张卡悄悄出现，一天最多一次，也遵守你设的安静档，和她其他的卡片一样。

总额只算日志里记了价格的模型调用。语音合成、辅助的小模型和网页搜索都不在里面，所以实际的一天会比卡片上写的略高一点。

<!-- shot: 刘海下方一张无声卡片，写着“今天的花费超过上限了”和“今天已花 …，上限 …”一行；示例数字 -->

Design notes: [One notch card when a day's recorded spend first crosses its limit](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0173-one-notch-card-when-a-days-recorded-spend-first-crosses-its-limit.md), [The Usage page spends a Codex reset after two clicks](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0048-the-usage-page-spends-a-codex-reset-after-two-clicks.md)
