---
title: "设计笔记"
description: "Jarvis 背后的四个决定，各自讲清量过什么、哪个方案落了选：让她被打断、长答案、记得越多还能保持快、声音里的爆音。"
---

下面每个决定在 [`docs/adr`](https://github.com/samsara0xgg/Jarvis/tree/main/docs/adr) 里都有一份记录，记录里必须有一节“Alternatives rejected”（被否决的方案）。这里挑了四个，写成小故事。数字都出自那些记录。

## 让她被打断

**问题。** 第一版 [jarvis-legacy](https://github.com/samsara0xgg/jarvis-legacy) 有打断功能，后来一量，它一次都没触发过。现在你可以随时插话。可麦克风就在扬声器旁边，会听到她自己的声音，一声咳嗽也和一句命令一样容易被当真。

**量到的。** “Hey Jarvis，停”这类先唤醒再说关键词的打断，在 MacBook 的扬声器上每分钟冒出 7.53 个误报，目标是 0.5。第一版“一听到有人说话就停”的做法，在 reSpeaker 上没人说话时也停了三次（2026-09-25），都是 -40 dBFS 附近的短声音。

**决定。** 有人开口时先把她压低（增益 0.2）；声音里攒够 0.8 秒人声，她就停在原处；接下来由语音识别听到的内容来定：哼一声，她接着说；“停”，她就停；别的话，她停下并当作新的一轮来回答。回声看当前打开的是哪支麦克风：reSpeaker XVF3800 在板子上就把她的声音去掉了，软件就不再动它；其他麦克风走软件回声消除（WebRTC AEC3）。

**落选的。** 一开口就停：一声咳嗽、一个“嗯”或者电视声，就会截断她的回答，还取消正在生成的内容。只看人声时长：一长串“嗯嗯嗯”会让她停下，一声很快的“停”反而停不住。

**结果。** 五次真机运行。最后一次里，五个“嗯”全都放过去了，“pause”和“可以啦”让她停下且没有当成一轮来答，停的时候也不再爆音了（之前会，加了 20 毫秒的淡出才好）。代价是：一声短短的“停”现在要从第一个音算起 0.5 到 0.7 秒才停。真扬声器上的回声抑制效果还没量过。

决定记录：[0100](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0100-jarvis-yields-to-speech-over-her-and-its-words-decide.md)、[0041](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0041-wave-mode-listens-without-a-wake-word.md)、[0143](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0143-echo-cancellation-follows-the-open-microphone.md)

## 长答案

**问题。** 模型写出来的答案是给人读的，原样念出来很拖。2026-09-24 查一次天气，英文要说 729 个字符，有的答案能播上 49 秒。有一次测试，一个 471 字符的答案播了大约 39 秒，作者在 20.7 秒时插了话。

**量到的。** 第一个办法是再发一次模型请求，把长答案改写成一到三句。每个长答案要多花 1.3 到 1.9 秒，而且屏幕上的字和耳朵听到的不是同一段话。让模型自己标出口播和文档的标签也不行：一次测试里，8 个答案有 6 个漏了标签。

**决定。** 不再发第二次请求：这一轮的每个请求都带一个严格的 JSON 结构。`spoken` 是一两句话，边流出来边说；`written` 放列表、时间和链接，只给屏幕看。拿 20 轮真实对话做探测，20 次结构全部合法，`spoken` 总是先出来，第一个字的延迟中位数 0.75 秒，纯文本是 0.66 秒。

**落选的。** 把提示词写得更严（弱一点的模型照样不听）；用代码截断文本（作者否决了）；保留第二次请求（每轮 1.3 到 1.9 秒）。

**结果。** 屏幕上的字就是她说的字，随着她的声音逐字亮起；写出来的部分在她说完后出现在下面。记录里也写了弱点：`written` 可能编造细节（有一次编出了示例命令），所以这个开关默认关着，等在真实对话里观察够了再说。

决定记录：[0114](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0114-a-structured-spoken-answer-carries-spoken-then-written.md)、[0040](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0040-a-spoken-answer-is-its-spoken-form.md)

## 记得越多，还能保持快

**问题。** 每一轮在调用模型之前，都要把整个事件日志解码一遍。在真实的运行轨迹上，发出请求前的等待从 1.6 万条事件时的 0.16 秒涨到了 4.86 万条时的 1.36 秒，而日志只会越来越长。

**量到的。** 在真实日志的一份拷贝上（48,952 条事件），读一次大约 0.54 秒，几乎全花在解码上。一轮在第一个模型请求之前至少要读两次。

**决定。** 从一个高水位线往后折：把折好的状态留在内存里，之后只读新增的事件。读取方只有在能证明这份状态是自己所见日志的前缀时才用它，否则就从头折。空闲时，每读 100 次，后台会拿它和一次全量折叠对一遍。

**落选的。** 只要最新事件编号没变就缓存整份快照：一轮会先写入自己的输入，所以恰恰在最贵的那几次读取上落空。只折最近一段窗口：有些投影依赖任何时候的事件。物化成表：读取方就要开始写库了。

**结果。** 结果和全量折叠一样，这既由构造保证，也有测试；热路径要是又折了整个日志，金丝雀测试会失败。另外两处延迟来自服务商这一头。口播的那几轮向 OpenAI 要它的快速档（16 次付费调用），首个 token 的中位数从 1.04 秒降到 0.68 秒，价格大约是两倍。语音服务商给每段音频的头尾都垫了静音（878 段录音里，开头中位数 182 毫秒，结尾 261 毫秒），每个衔接处约 440 毫秒的空白；只裁安静的采样，每个答案大约早 340 毫秒播完。这两项都要手动开启，随仓库发的配置里是关的。

决定记录：[0164](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0164-the-decision-snapshot-folds-the-log-from-a-high-water-mark.md)、[0166](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0166-a-spoken-turn-asks-openai-for-its-fast-service-tier.md)、[0165](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0165-the-spoken-sample-timeline-drops-provider-silence-at-start-and-junctions.md)

## 音频里的爆音

**问题。** 回答里偶尔有很轻的“啪”声，看着像流式传输的 bug：音频块之间有接缝，或者重采样出了问题。

**量到的。** 都不是。在真实录到的 99 个段与段的衔接处，相邻采样的最大跳变是 0.0000，不管用每段各自的重采样器还是共用一个。接着把同样的句子用 MiniMax 的音量设置 1、2、3 各合成一遍（2026-09-07）：响度（RMS）线性上升，可峰值在 3 的时候只到了 2.8 倍。是服务商自己的限幅器在压响的音节，爆音就是这么来的；到了 5，它的 16 位输出直接削顶。

**决定。** 服务商的音量保持默认的 1，响度在我们这边调。MiniMax 的音量设到 1 以下并不会让返回的音频变轻（2026-09-25 量过），所以她的播放音量是我们自己播放器里的一个增益。

**落选的。** 在接缝上动手：衔接处的跳变本来就是 0.0000，换成共用重采样器什么也不会改变。自己加一个限幅器或去爆音器：声音在到我们手里之前就已经坏了，我们这边什么都补不回来。

**结果。** 音量是 1 的时候声音是干净的，想更响就调 Mac 的输出音量。后来她也可以按你的话调大声音，一次乘 1.4 倍或 2 倍，超过 200% 要先等你说一声好。

决定记录：[0165](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0165-the-spoken-sample-timeline-drops-provider-silence-at-start-and-junctions.md)、[0052](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0052-the-settings-page-writes-a-file-laid-over-the-yaml-at-boot.md)、[0174](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0174-her-voice-volume-and-speed-are-set-by-the-model-through-one-tool.md)；音量那组测量写在 [`config/jarvis.yaml`](https://github.com/samsara0xgg/Jarvis/blob/main/config/jarvis.yaml) 的 `tts_volume` 注释里。
