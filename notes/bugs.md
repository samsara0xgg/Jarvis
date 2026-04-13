# 小月 Bug / 优化修复表

> ✅ = 已修复  ⏳ = 进行中  ❌ = 未开始  🚫 = 等硬件/不做

---

## P0 — 实际体验问题（用户反馈）

### 1. 延迟问题（最严重）
- ✅ 统一 Groq 路由+回复单次调用 — 云端对话 2-3s → ~600-800ms
- ✅ info_query 模板化 — 天气/股票/新闻跳过 LLM 转述 省 ~1500ms
- ✅ TTS 高频句预缓存 — 启动时预合成常用短句
- ✅ 升级慢模型时播 filler — "嗯，让我想想" 立即播放

### 2. 三层路由出错率过高
- ❌ L1/L2/L3 路由复杂度不够真实应用，大部分请求实际都落到云端 LLM
- ❌ 意图路由 confidence 阈值/分类准确度需要重新评估

### 3. LLM 模型选择优化
- ✅ LLM preset 系统 — 默认 Groq fast，支持运行时切换
- ✅ ModelSwitchSkill — 语音切换模型 + 单次升级关键词

### 4. Memory 系统
- ⏸️ 暂缓，目前仍为验证阶段

### 5. 小 Bug
- ✅ 再见/告别后 ~10% 概率重新触发录音 — stream.start()移到farewell分支后 + detector.reset() + drain缓冲
- ✅ Tiffany 蓝等颜色指令失效 — COLOR_XY_MAP加常用色 + 路由传原始颜色 + 本地可解析时不绕云端

---

## P1 — 代码审查发现

## jarvis.py

- ❌ handle_text() 和 _handle_utterance_inner() ~300 行重复路由逻辑，应抽取共用方法
- ❌ _NEEDS_LLM_ACTIONS fallback 逻辑复杂，设备状态注入写死在主循环
- ❌ _cancel_current() 只能 cancel Future，无法 kill afplay 子进程实现真正打断

## core/llm.py

- ❌ _CHARS_PER_TOKEN = 3 对中文偏低（应 1.5-2），history 过度截断
- ❌ 流式模式检测 tool_use 后 fallback 非流式，双倍 API 调用

## core/tts.py

- ❌ _play_audio_file() 用 subprocess 调 afplay/ffplay，无法优雅中断
- ❌ 缓存只对 MiniMax 生效，最贵的 OpenAI TTS 反而没缓存
- ❌ Azure SSML escape 只用 xml.sax.saxutils.escape，特殊字符边界情况

## core/speech_recognizer.py

- ❌ Confidence 只有 0.1/0.9 两档，比较粗糙
- ❌ 没有语言自动检测逻辑（固定 zh）

## core/intent_router.py

- ❌ 缓存 key 只做标点 strip，"开灯"/"开一下灯" 不命中
- ❌ Groq/Cerebras 都 down 时返回 confidence=0.0，无任何缓存

## memory/manager.py

- ✅ _call_openai_json() 和 _call_llm_extract() 复用 requests.Session
- ❌ Profile 重建遍历所有 active memories，O(n) 无缓存
- ❌ _RELATION_KEYWORDS 只覆盖中文亲属关系，英文关系词缺失
- ❌ Episode digest 简单拼接，无 LLM 摘要

## memory/direct_answer.py

- ❌ _is_question() 规则匹配，"我昨天说的那件事" 漏判
- ❌ 只有中文问题标志词，英文问句漏

## 测试 (9 failed)

- ❌ test_wake_word (×2) — openwakeword 导入失败
- ❌ test_tts TTS speed — OpenAI TTS speed 参数
- ❌ test_memory_store episode dedup — Jaccard 阈值/逻辑
- ❌ 其余失败测试

## 其他模块

- ❌ LearningRouter — config/compose 模式落 LLM
- 🚫 自定义唤醒词 "小月" — 需 openwakeword 训练

## 技能系统

- ❌ SkillFactory 生成的技能加 pending_review 状态追踪（可用但待审查）
- ❌ RPi ~/.claude/CLAUDE.md 写专用 skill 生成指令（代码风格/安全/测试/接口规范）
- ❌ 加 WebSearchSkill（DuckDuckGo，零配置）
- ❌ RemoteDevSkill — 小月通过 SSH 控制 Mac 端 CC/开发环境（跑测试/改代码/git 操作/rsync 同步）
- ❌ ClaudeSkill — 通用语音→CC 接口（"帮我问Claude..."）

## 行为学习 (T2 方向)

- ❌ 消费 behavior_log — 技能成功率、常用指令、使用模式
- ❌ 用户画像定期重建 — profiles + memories + behavior_log 合成
- ❌ FTS5 会话搜索 — ConversationStore 加全文搜索

## 等硬件

- 🚫 F3 情境感知 — LD2450 + DHT22 + BH1750
- 🚫 F7 显示屏 — ST7789/GC9A01
