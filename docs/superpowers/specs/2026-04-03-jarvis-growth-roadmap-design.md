# 小月成长路线图 — 设计文档

> 目标：让小月成为一个有完整记忆、能学习新技能、越来越懂用户的成长型管家。

## 现状摘要

**已完成：** SenseVoice ASR + 声纹 + 5引擎 TTS + 情感映射 / 意图路由（三层 fallback）/ 小月人格 / 13 个 skills / 自动化规则（keyword/cron/once）/ 自诊断熔断器 / 新闻股票 / Dashboard / 远程控制 / 长期记忆系统（MemoryManager：embedding + 3-tier query + LLM 提取 + 去重 + profile）

**已集成但未端到端验证：** `jarvis.py:446` 调用 `memory_manager.query()` 注入 prompt；`jarvis.py:569` 对话结束调用 `save()`。底层管线存在但未在真实场景中验证全链路。

**已知架构问题：** `build_personality_prompt` 的 `preferences` 参数和 `memory_context` 做同一件事但互不关联；save() 触发时机（"对话结束"）在语音助手中未明确定义；缺少自然语言记忆修正路径。

**硬件状态：** AliExpress 采购中（RPi5 配件 + ESP32 + 传感器），预计 4 月中到货。

---

## 路线总览

```
时间线：
          现在                    硬件到货(4月中)
           |                         |
T0 ████████████                      |
           |                         |
T1    .....████████████              |
           |                         |
T3.A       |    .....████████        |  <- ESP32 固件可提前写
           |                         |
T2              .....█████████████   |
           |                         |
T3.B                                 ████████
T3.C                                    .....████████

████ = 主要开发
..... = 数据积累/准备期
```

---

## T0: 记忆系统从"能跑"到"可信赖"

### T0.1 管线打通 — 数据流没有断点

**保存侧（对话 -> 持久化）：**
- 明确"对话结束"定义：静默超时（5 分钟无交互）或用户说"晚安/再见" -> 触发 save()
- 验证完整路径：conversation messages -> `save()` -> LLM extract -> dedup -> `store.add_memory()` -> embedding 写入
- 验证 profile 自动构建：提取到 identity/preference -> `_rebuild_profile()` -> 下次 query 能取到
- 验证 episode_summary 被正确存储和注入

**检索侧（用户说话 -> 记忆注入）：**
- 验证完整路径：`query(text, user_id)` -> profile + episodes + memories -> `_format_memory_context()` -> 注入 personality prompt
- 修复 preferences 重复路径：统一为只用 `memory_context`（`<memory>` 块已包含 profile），删掉 `build_personality_prompt` 中未使用的 `preferences` 参数
- 验证 embedder 加载时机：确认 lazy load，不阻塞启动

**端到端集成测试：**
- 模拟 3 轮对话（包含"我喜欢拿铁"），触发 save，然后新对话问"我喜欢喝什么"，验证 memory_context 里包含"拿铁"

**行为日志埋点（为 T2 预备）：**
- 新增 `behavior_log` 表（SQLite，append-only）：
  ```
  behavior_log:
    timestamp, user_id, event_type, detail
  ```
- event_type：skill_call / conversation / suggestion_response / correction
- 在 skill 执行、对话开始/结束、记忆修正时写入
- T0 只实现收集，T2 实现分析

### T0.2 记忆质量 — 存的对、取的准、改得了

**提取质量：**
- 用 5-10 种真实对话场景测试 LLM 提取（事实陈述、偏好表达、事件提及、无用闲聊、混合）
- 验证去重生效：说两次"我喜欢拿铁"不会存两条
- 验证更新生效："我以前喜欢拿铁，现在改喝美式了" -> 旧记忆被 supersede

**自然语言记忆修正：**
- 用户说"不对，我不喜欢拿铁" -> 意图路由识别为"记忆修正"意图
- 检索相关记忆 -> 标记为 superseded -> 存入新记忆
- 不需要"忘掉xxx"命令式语言，自然对话中的否定/纠正就应该触发更新
- 实现：在 save() 的提取 prompt 中加入 `correction` 字段，如果用户纠正了之前的信息，标记旧记忆

**检索相关性：**
- 测试语义检索准确度："喝什么" 应该命中 "喜欢拿铁"
- 测试 category 过滤效果
- 调优 threshold（当前 same_cat=0.55, cross_cat=0.7，根据实测调整）

### T0.3 记忆使用 — LLM 真的"懂"记忆

**Prompt 引导优化：**
- 在 `<memory>` 块后加使用指引：
  ```
  这些是你对用户的了解。在对话中自然地运用这些信息，
  像朋友一样自然地想起来，不要像读档案一样列举。
  如果记忆和当前话题无关，不要强行提起。
  ```
- 待关心项（pending）的引导：自然关心，不像闹钟提醒

**注入策略：**
- 控制注入总量：profile + episodes + memories 总计不超过 ~500 tokens
- 相关性优先：和当前话题相关的记忆排前面
- 时效性衰减：近期记忆权重高于远期，但高 importance 的永远保留

**Level 1 直接回答（快路径）：**
- 在意图路由前加记忆检索
- 匹配条件严格：embedding 相似度 > 0.85 + category 是 preference/identity/knowledge
- 命中 -> 模板回答（"你跟我说过，你喜欢拿铁"），不走 LLM，延迟 < 100ms
- 未命中 / 置信度不够 -> 正常路由（记忆仍注入 prompt 给 LLM 参考）

### T0.4 运维保障

- `maintain_all()` 挂到现有 scheduler（每天凌晨跑一次）
- Dashboard 加记忆面板：总数、今日新增、最近提取的记忆预览
- 日志确认：每次 save 记录提取了几条、更新了几条、跳过了几条

### T0 验收标准

对小月说"我喜欢拿铁"，下次新对话问"我喜欢喝什么"，她能自然回答"你之前跟我说过你喜欢拿铁呀"。说"不对我改喝美式了"，再问一次，回答更新为美式。

---

## T1: 技能学习 — 三种学习模式

### 学习意图路由

在现有意图路由之前加一层判断：
```
用户输入
  -> 是教学/学习意图吗？（关键词："以后"、"学会"、"记住每次"、"帮我加一个"）
  -> 是 -> 分类：配置 / 组合 / 创造
      -> 配置：直接匹配现有 skill + 参数
      -> 组合：匹配多个 skill + 调度
      -> 创造：走 Claude Code 流程
  -> 不是 -> 正常意图路由
```

### T1.1 配置型（最轻）

用户给现有 skill 设快捷方式或默认参数。

```
"以后我说收盘就帮我查 NVDA 和 AAPL"
  -> 识别：这是 realtime_data skill + 固定参数
  -> 存为 alias rule：trigger="收盘" -> skill=realtime_data, params={symbols: [NVDA, AAPL]}
  -> 不写任何代码
```

存储：扩展现有 `automation_rules`，加一种 `type: "skill_alias"` 规则。

### T1.2 组合型（中等）

把现有 skills + 调度串起来。

```
"每天早上8点帮我查天气和股票"
  -> 识别：weather skill + realtime_data skill + cron 触发
  -> 存为 cron rule，action 是串行执行两个 skill
  -> 不写新代码，用现有 scheduler + skill 组合
```

存储：现有 `automation_rules` 的 cron 类型，action 支持 skill 列表。

### T1.3 创造型（重 — Claude Code 技能工厂）

现有 skills 覆盖不了，需要新代码。

**流程：**
```
用户："学会查航班信息"
  -> 小月确认："你想让我学会查航班，我去学一下？"
  -> 准备上下文包：
      - skills/__init__.py（Skill ABC 接口定义）
      - 2 个现有 skill 作为范例（挑最相似的）
      - 用户需求描述
      - 约束（安全边界、编码规范）
  -> 调用 Claude Code CLI：
      claude -p "按照以下模板写一个 skill..." --allowedTools Edit,Write,Bash
      输出到 skills/learned/<name>.py + tests/test_learned_<name>.py
  -> 验证管线（顺序执行，任一步失败终止）：
      1. ruff check（语法/风格）
      2. 安全扫描（禁止 os.system/eval/exec、禁止写 core/）
      3. 依赖检查 -> 缺的包问用户能不能装
      4. pytest（CC 写的测试）
      5. dry-run execute（mock 输入跑一次）
  -> 全部通过 -> importlib 热加载 -> 注册到 SkillRegistry
  -> 告诉用户"学会了，要试试吗？"
```

**安全边界：**
- learned skill 运行在受限环境：无文件系统写入（除 `data/`）、无 subprocess
- 网络请求：首次使用新域名时问用户
- CC 生成的代码经过安全扫描才能注册

### T1.4 技能管理

- `skills/learned/` 目录，启动时自动扫描加载
- 每个 learned skill 带 metadata：谁教的、什么时候学的、用了几次
- 使用记录存入记忆系统（和 T0 联动）、写入行为日志（为 T2 提供数据）
- `"你都会什么"` -> 列出所有 skills（内置 + 学到的）
- `"你什么时候学的查航班"` -> 从经历记忆中检索
- 可禁用/删除/更新：`"忘掉查航班"` / `"更新一下查航班的技能"`
- 技能版本：更新时保留旧版，可回滚

### T1 验收标准

说"以后我说收盘就查 NVDA"，下次说"收盘"自动查股票。说"学会查航班"，CC 生成 skill 并通过审查，说"查明天从温哥华到北京的航班"能返回结果。

---

## T2: 行为学习 — 统计为主，LLM 为辅

### T2.1 模式检测（统计方法，不用 LLM）

三种检测器，每天凌晨跑一次（和 memory maintenance 一起）：

**时间模式检测器：**
- 统计每个 skill 在每个时段的调用频率
- 如果某 skill 在某时段连续 N 天（N >= 3）被调用 -> 时间模式
- 例：weather_skill 在 07:00-09:00 连续 5 天被调用

**序列模式检测器：**
- 统计 skill A 之后 5 分钟内调用 skill B 的频率
- 如果 P(B|A) > 0.6 且发生次数 >= 3 -> 序列模式
- 例：realtime_data 之后 80% 的时间会调 weather

**偏好漂移检测器：**
- sliding window：最近 7 天 vs 之前 30 天的偏好对比
- 如果某偏好出现频率变化 > 50% -> 候选漂移
- 不自动更新，先标记为"观察中"

检测结果存为 `pattern` 记录，带置信度和状态（detected / suggested / accepted / rejected）。

### T2.2 建议引擎

**建议时机（不打断正常使用）：**
- 对话开始时的空闲窗口（打完招呼后）
- 对话结束后的"顺便一提"
- 绝不在用户正在执行操作时插入

**建议方式：**
- 统计结果 -> LLM 润色成自然语言（LLM 唯一的用武之地）
  ```
  输入：{pattern: "weather at 08:00, 5 consecutive days"}
  输出："我注意到你最近每天早上都会问天气，要不要以后自动帮你播报？"
  ```
- 用户同意 -> 自动创建 T1 的组合型/配置型规则
- 用户拒绝 -> 标记 rejected，同类模式 30 天内不再建议

**频率限制：**
- 每天最多 1 条建议
- 连续被拒绝 3 次 -> 暂停建议一周

### T2.3 偏好漂移处理

| 信号强度 | 处理 | 例子 |
|----------|------|------|
| 弱（<2周） | 只记录不行动 | 连喝了 3 天美式 |
| 中（2-4周） | 主动确认 | "你最近都在喝美式，换口味了？" |
| 强（>4周 or 用户确认） | 更新 profile | 自动更新偏好记忆 |

### T2 验收标准

连续一周每天早上问天气后，小月在某天对话间隙主动建议"要不要以后自动帮你播报早间天气？"接受后自动执行。

---

## T3: 环境感知 — 分层设计

### T3.A ESP32 固件（独立技术栈，可与 T1 并行）

**技术：** MicroPython 或 Arduino（到手后根据性能决定）

**职责：** 采集传感器原始数据 -> MQTT 发布

**传感器驱动：**
- LD2450：人体存在 + 距离 + 多目标，UART 协议解析
- DHT22：温湿度，每 30 秒采一次
- BH1750：环境光，每 30 秒采一次

**MQTT topic 设计：**
```
jarvis/sensor/{node_id}/presence   -> {targets: [{x,y,distance}], count: N}
jarvis/sensor/{node_id}/climate    -> {temp: 23.5, humidity: 60}
jarvis/sensor/{node_id}/light      -> {lux: 350}
jarvis/sensor/{node_id}/heartbeat  -> {uptime: 3600, free_mem: 80000}
```

**心跳 + OTA 更新机制。**

### T3.B 数据聚合（RPi5 端，硬件到货后）

原始传感器数据 -> 有意义的事件，解决数据量爆炸问题：

```
LD2450 每秒数据 -> 聚合为状态变化事件：
  "有人进入房间" / "房间无人已 30 分钟" / "有人但不在常用位置"

DHT22 每 30 秒数据 -> 聚合为异常事件：
  "温度低于 18C" / "湿度超过 70%"（阈值可配置）
  正常范围内不生成事件

BH1750 -> 聚合为：
  "天黑了" / "天亮了" / "光线突变"（窗帘/灯变化）
```

- 事件发到 event_bus（已有基础设施）
- 不直接写记忆，由情境引擎决定什么值得记

### T3.C 情境引擎（整合层，依赖 T0+T2+T3.B）

综合环境事件 + 时间 + 记忆 -> 情境状态：

```
环境：房间有人、22C、天黑了
时间：晚上 10 点
记忆：用户通常 11 点睡
行为：用户今天没说过晚安
-> 情境状态：evening_wind_down
-> 影响：语速放慢、音量降低、主动问要不要关灯
```

- 情境状态注入 personality prompt 的 `<situation>` 块（已有接口）
- 环境数据中的长期模式存入记忆（"用户通常 23:00 离开房间"）

### T3 验收标准

晚上 10 点房间有人时，小月语速自动放慢、主动问要不要关灯。白天温度低于 18C 时主动提醒"今天有点冷"。

---

## F 系列映射

| 原 Feature | 归入 | 说明 |
|-----------|------|------|
| F0 新闻/股票 | 已完成 | — |
| F1 意图路由 | 已完成 + T0 | T0 加 Level 1 记忆检索 |
| F2 人格 | 已完成 + T0 | T0 优化记忆注入 prompt |
| F3 情境感知 | T3.B + T3.C | 传感器 -> 聚合 -> 情境 |
| F4 主动通知 | T2 | 行为学习的建议引擎是其进化版 |
| F6 自然语言自动化 | 已完成 + T1 | T1 扩展三种学习模式 |
| F7 OLED | T3 | 到货后验证 |
| F8 自诊断 | 已完成 | — |
| F9 开发者模式 | T1.3 | CC 技能工厂是 F9 的进化 |
| F10 行为学习 | T2 | — |

---

## 技术约束

- RPi5 4GB 内存限制：embedding 模型 ~140MB，需要 lazy load
- 云端 LLM 成本控制：Level 1 直接回答 + 行为检测用统计不用 LLM
- Python 3.11，所有新代码遵循 CLAUDE.md 编码规范
- 新 skill 继承 `skills.Skill` ABC
- 配置从 config.yaml 读，不硬编码
