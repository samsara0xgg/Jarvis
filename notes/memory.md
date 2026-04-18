# Jarvis 记忆系统重设计 — 会话总结

**日期**：2026-04-15  
**工具**：Hermes Agent (Opus 4.6) · 5h39m · 120 messages  
**调研报告**：`notes/mastra-research-{01..06}-*.md`（6 份详细报告另存）

---

## 一、七模块架构（已确定）

```
① 记忆块 ── "懂我"
   · 记: OM append-only observation stream
   · 读: stable prefix 注入 prompt（全塞·无筛选）
   · 推断: LLM 读 prompt 时现场推（零代码·OM+长context天然具备）
   · 主动: reflection + trigger（后期·低优先级）

② 工具块 ── "好用"+"聪明"
   · 已有: 14 内置 skill + device 调用
   · 学习闭环: trace → hotspot 检测 → L1 YAML / L2 tactical → shadow → live
   · 分层: L1 声明式 / L2 小LLM+工具白名单 / L3 Cloud LLM 兜底

③ 感知块 ── 入口 ✓ 稳定不改
   ASR(SenseVoice INT8) / 唤醒(openwakeword) / VAD(Silero) /
   声纹(ECAPA) / 打断(XVF3800 待到货)

④ 表达块 ── 出口 ✓ 稳定不改
   TTS 5层fallback / 情感映射 / 双线程流式

⑤ 人格块 ── 常量·只人工改
   personality.py · 包裹每次 LLM 调用

⑥ 路由块 ── 需整合
   热路径 6 层穿透: farewell → DirectAnswer → L1 Skill → Local →
   L2 Skill → Cloud LLM
   现状分散（intent router / DirectAnswer / farewell 各自独立）

⑦ 数据底座
   SQLite + FastEmbed · behavior_log 升级为 trace
```

连线图：
```
用户说话 → ③感知 → ⑥路由(6层穿透) → ②工具/①记忆读 → ④表达 → 用户听
                                         ↓ 每轮写 trace
                                    ⑦数据底座(trace表)
                                         ↓ 异步
                                    ①记忆写(Observer) + ②学习(cold path)
```

---

## 二、OM Pure 方案（已确定为主路线）

核心赌注：Grok 在 30k context TTFT ~500ms + cache 100% 命中 → 把"记忆"全塞 prompt，放弃复杂 RAG。

```
Layer 0 · DirectAnswer (~50ms)   ← FastEmbed 快路径·高频问答
Layer 1 · Stable Prefix (~500ms) ← 主力·personality + profile + OM stream + 最近10轮
Layer 2 · Deep Dive (100k)       ← Claude Haiku 4.5·全历史检索（Phase 1 不实现）
```

写入侧（极简）：每轮 → trace → Observer 抽取 observation → append stream

关键洞察：**"推断"不是独立模块，是 LLM+OM 的天然能力**。只有跨时段+主动提醒才需要写代码。

容量估算：日均 50-100 turn × 1-3 obs/turn × 50 token/obs → **约 100 天到 25k token**。前 3 个月无需窗口策略。

---

## 三、记忆块 Schema（敲定版·聚焦"记+读"）

```sql
-- 表 observations (OM stream)
id              INTEGER PK
chunk_id        TEXT
created_at      TIMESTAMP
content         TEXT  -- 整段 markdown: Date 头 + emoji 行
source_turn_id  INTEGER FK→trace
superseded_by   INTEGER  -- 留字段·后期纠正用·默认 null

-- 表 trace (原 behavior_log 升级)
id              INTEGER PK
session_id      TEXT
turn_id         INTEGER  -- 会话内递增
created_at      TIMESTAMP
user_text       TEXT
assistant_text  TEXT
user_emotion    TEXT  -- SenseVoice 情感
tts_emotion     TEXT  -- TTS 情感
path_taken      TEXT  -- farewell/direct_answer/l1_skill/local/l2_skill/cloud_llm
tool_calls      JSON  -- (name, args, result, ms)
llm_model       TEXT
llm_tokens_in   INTEGER
llm_tokens_out  INTEGER
latency_ms      INTEGER  -- end-to-end
outcome_signal  INTEGER  -- null/-1/0/+1
outcome_at_turn_id INTEGER
```

推后不做：reflection_generations 表 / 白名单 diff / 纠正快通道 / 压缩策略

---

## 四、记忆块子能力

### 子能力 1 · 记（写入·异步·不阻塞热路径）
- 每轮结束 → 拉 user_text + assistant_text + tool_results
- Observer LLM 用 function calling 抽 0-N 条 observation
- INSERT INTO observations + 计算 embedding（留作 escape hatch）
- 失败 → log warn → 跳过

### 子能力 2 · 读（热路径·拼 prompt）
```
stable_prefix = personality
              + core_profile
              + ALL observations (时间正序·emoji+timestamp+text)
              + [ALL live reflections (后期)]
tail          = 最近 10 turn + 本轮 user input
```
拼 prompt < 5ms · LLM 端 cache 命中省 90%+

### 子能力 3 · 推断
- 即时推断：零代码·LLM 读 stable_prefix 时自动发生
- 跨段推断：走 reflection（后期）

### 子能力 4 · 主动（后期·低优先级）
- Reflection 生成（周日 03:00 冷路径）
- Morning briefing 注入
- Feedback 回路（α/β 质量追踪）

对外接口：
```python
memory.write_observation(turn_id) → async   # 子能力1
memory.build_stable_prefix() → str          # 子能力2
memory.run_weekly_reflection() → int        # 子能力4·后期
memory.feedback(reflection_id, signal)      # 子能力4·后期
```

---

## 五、Stable Prefix 示例

```
You are Jarvis, Allen's personal voice assistant...
[personality + core profile]

<observations>
Date: 2026-04-10
* 🔴 (09:15) Allen 偏好中文交流
* 🔴 (20:00) Allen 在做 Jarvis 项目·部署目标 RPi5

Date: 2026-04-15
* 🟡 (14:30) 用户语气疲惫·在调灯时说"累死了"
* 🔴 (14:30) 用户偏好客厅灯用暖黄色（2700K / #FFB36B）
* ✅ (14:30) 客厅灯已调为暖黄
</observations>

Newer observations supersede older ones. Reference specific details when relevant.

--- 最近 10 轮对话 ---
[user] ...
[assistant] ...
--- 本轮 ---
[user] 晚上好·再把客厅灯调一下吧
```

---

## 六、Mastra OM 调研关键发现（5 个翻案）

> 详细报告见 `notes/mastra-research-{01..06}-*.md`

### 翻案 1 · Mastra OM 生产成熟度 LOW
- OM 发布仅 2 个月（2026-02-09）· Mastra GA 才 6 天
- Reflector 在 ≈40k tokens 陷无限循环（GitHub #14110 · #15062 仍 Open）
- OOM 崩溃（#13389）
- → 不能 in-stream rewrite · 必须有回滚路径

### 翻案 2 · Reflector 无硬偏好白名单
- 整个 5 级压缩 prompt 没有过敏/健康/安全/金额白名单
- Level 4 允许"合并相关观察为 generic"
- → 家庭语音助手需自己加白名单 diff 审核（后期做 reflection 时加）

### 翻案 3 · Mastra Reflection 实际是独立表+代际历史
- 每次 reflection 生成新 generation record（新 UUID · generationCount++）
- 老代保留 · 只切换 active_observations 指针
- → 我们的"γ 独立小表"方案反而更贴近 Mastra 实际做法

### 翻案 4 · Grok-4.20 做中文 Observer/Reflector 原以为出局（被实验推翻）
- ReLE 中文评测 47.6% 垫底
- 但 Observer bench 实测 recall 0.87 排第 3，性价比全场最佳
- → ReLE benchmark 与 Observer 抽取任务相关性低

### 翻案 5 · optimizeObservationsForContext 是字符级剥 emoji，非行级过滤
- 只剥 🟡🟢 字符 · 行和子要点全保留
- OM 准确率主要来自 prompt 工程 + 长 context LLM · 不是过滤

---

## 七、Observer Bench 实验结果

### 实验设计
- 20 条中文家庭对话 fixture（智能家居/偏好/时间/状态/情感/多实体/任务完成）
- 每条带人工标注的 expected observations（ground truth）
- 8 个候选模型 · 统一 Tool Use / Function Call 路径
- 指标：F1（halluc-aware precision）· Recall · Halluc Rate · Latency · Cost · Priority Accuracy

### 最终排名（清洁数据）

| # | Model | F1 | P | R | Halluc | p50 | p95 | $/100 | Priority |
|---|---|---|---|---|---|---|---|---|---|
| 🥇 | grok-4-1-fast | 0.91 | 0.95 | 0.89 | 5% | 5.0s | 7.4s | $0.034 | 0.82 |
| 🥈 | gemini-2.5-flash | 0.89 | 0.95 | 0.86 | **0%** | 2.5s | 5.2s | $0.066 | **0.89** |
| 🥉 | haiku-4-5 | 0.88 | 0.95 | 0.84 | 5% | 1.9s | 3.1s | $0.31 | 0.76 |
| 4 | **grok-4.20** | 0.88 | 0.93 | 0.87 | 5% | **3.4s** | **4.8s** | **$0.031** | 0.79 |
| 5 | deepseek-chat | 0.87 | 0.88 | 0.88 | 5% | 4.8s | 9.4s | $0.057 | 0.72 |
| 6 | gemini-3-pro | 0.83 | 0.88 | 0.82 | 5% | 8.6s | ⛔32s | $0.21 | 0.78 |
| 7 | gpt-5-mini | 0.77 | 0.78 | 0.78 | 10% | ⛔20s | ⛔25s | $0.11 | 0.57 |
| 8 | llama-3.3-70b | 0.72 | 0.80 | 0.69 | 5% | **0.95s** | 1.4s | $0.090 | 0.57 |

### 反直觉发现
1. **Grok 中文没垫底** — grok-4-1-fast F1 全场最高 · grok-4.20 第 4 + 最便宜 + 最快最稳
2. **Gemini 2.5 Flash 没碾压** — F1 第 2 · 但 Halluc=0% + Priority Acc=0.89 全场唯一双冠 · "少而精"
3. **DeepSeek 真的能打** — F1 第 5 · 中文本土优势 · 但 p95=9.4s 偏慢
4. **Claude Haiku 排第 3 没崩** — 但贵 10× · 性价比差

### 决定
- **主 Observer: grok-4.20-0309-non-reasoning** — F1 0.88 · 最便宜 $0.031 · 最快最稳 p50=3.4s/p95=4.8s · 单 provider（与主对话统一）
- **Fallback: gemini-2.5-flash** — 0% halluc + 0.89 priority acc · 不同 provider 抗故障 · 速度 2.5s

### Observer 延迟不影响体验
Observer 是 cold path · 异步 · 对话结束后才跑 · 3.4s 发生在用户听完回复之后。
唯一风险：快速连续对话时新 obs 可能落后一轮（缓解：user_text 在"最近10轮"里 · Grok 可从原话推断）。

### 5% Halluc 的真相
全部来自 fx_017 的"大后天/后天"纠正场景 · 模型合理记录了"嘴瓢"纠正 · 禁用词列表设太严 · 实际接近 0%。

---

## 八、LLM 池（已确定）

| 角色 | 模型 | 理由 |
|---|---|---|
| 主对话 | Grok-4.20-non-reasoning | cache 100% · TTFT 488ms |
| Observer (记忆抽取) | Grok-4.20-non-reasoning | bench 实测 · 最便宜最快 |
| Observer Fallback | Gemini-2.5-flash | 0% halluc 防御 |
| Extractor (现有记忆) | GPT-4o-mini | 已调优 · function calling 稳 |
| 意图路由 | Groq Llama-3.3-70B | ~100ms · 保持现状 |
| Reflector (后期) | 待定 (Grok-4.20 起步 → 收集数据后评估) | |
| Fallback | Claude Haiku 4.5 | 深度回忆 100k 窗口 · Phase 1 不实现 |

---

## 九、Observer Prompt 设计决定

- **英文 prompt + 中文对话 + 中文 observation 产出**（方案 A）
- 直接用 Mastra 原文 instruction · 加 `output in Simplified Chinese`
- 理由：GPT-4o-mini / Grok 英文指令 + 中文内容是标准实践 · Mastra 250 行 prompt 是调优过的 · 翻译会丢失精确措辞
- 输出格式：**Function Call / JSON Schema**（不用 XML · 跨 provider 统一 · Jarvis 生产管线本来就是 function calling）
- 增补：SenseVoice 情感章节（Mastra 没有 · Jarvis 独有需求）
- 保留 Mastra 精华规则：assertion vs question / 时间锚定 / 原话保留 / 精准动词 / ✅完成标记 / USER ASSERTIONS TAKE PRECEDENCE

---

## 十、工具块 ② 讨论（刚开始·未完成）

已确认的展开顺序：
1. 现状盘点（14 内置 skill · intent router · SkillFactory v1 状态）
2. L1 YAML 格式定义（API 包装·声明式）
3. L2 Tactical 格式定义（小 LLM + 工具白名单·规划式）
4. trace → skill 编译 pipeline（hotspot 检测 → 编译 → shadow → live）
5. shadow → live 晋升机制
6. 路由判官 · hot path 穿透逻辑

工具块的三个子能力：
- **用**（热路径·已有 14 个 skill）
- **学**（冷路径·trace 学习闭环·SkillFactory v2 待重做）
- **判**（路由层·决定走哪层）

---

## 十一、6 个 P0 Bug（待修·与记忆无关）

1. 颜色 value=0 — intent router prompt 问题
2. LLM 幻觉执行 — tool-use 双倍播放
3. 多轮上下文丢失
4. info_query 误判 — _is_question 逻辑
5. 情感元认知
6. 回复超长

框架文结论：**这些 bug 没一个能靠记忆优化解决**，需要在记忆重设计之前或并行修复。

---

## 十二、Eval 方法论笔记

- Fixture 的 expected_observations 不可能穷举模型所有合理输出
- 最终采用 halluc-aware precision：`matched / max(matched, expected)` — 对多抽轻度扣分但不重罚
- Recall + Halluc Rate 是真信号 · F1 是综合参考
- 记忆系统 bench 只能做 "go/no-go + 参数初调" · 真正验证要生产用 2-4 周

---

## 十三、工具块现状地图

```
用户说话
  │
  ▼
ASR + 声纹 (并行)
  │
  ▼  13 步热路径·按最快优先逐层穿透
┌──────────────────────────────────────────────────┐
│ 1. Farewell shortcut  "再见" → 直出 ~120ms      │
│ 2. Escalation         "仔细想想" → 临时切        │
│ 3. Memory save        "记住X" → 直存             │
│ 4. Learning trigger   "学会X" → SkillFactory     │ ← v1 还活着
│ 5. Keyword rule       AutomationRule → 本地      │
│ 6. Memory query       向量检索补上下文            │
│ 7. DirectAnswer       embedding 精确匹配         │
│ 8. Intent router      Groq Llama-3.3-70B         │
│    ├─ conf≥0.90 + smart_home → LocalExecutor     │
│    ├─ conf≥0.90 + info_query → LocalExecutor     │
│    ├─ conf≥0.90 + time       → LocalExecutor     │
│    ├─ conf≥0.90 + automation → LocalExecutor     │
│    ├─ chat (路由自答)         → 直接出 text       │
│    └─ else                   → 穿透到 Cloud LLM  │
│ 9. Cloud LLM + tool-use      Grok-4.1-fast 主    │
│    └─ tool_executor = SkillRegistry.execute()     │
│       (14 内置 skill + 动态 learned skill)        │
│10. TTS 流式输出                                   │
│11. 存对话 + 异步 memory                           │
│12. behavior_log                                   │
└──────────────────────────────────────────────────┘
```

**15 个 skill：**
- 核心 9：SmartHome / Weather / Time / Reminder / Todo / SystemControl / Memory / Automation / ModelSwitch
- 可选 4：RealTimeData / Scheduler / Remote / Health
- 学到 1：exchange_rate.py（status=pending_review）
- 管理 1：SkillManagement（语音列/禁/删 learned skill）

**SkillFactory v1** 还活着（jarvis.py line 226 每次启动实例化）。用户说"学会汇率转换" → 后台线程调 claude CLI 生成 Python + 安全扫描 + pytest。v2 只有 7 份调研 notes · 零代码。

---

## 十四、工具块 v2 整体大框架

```
用户说话
  │
  ▼
ASR + 声纹
  │
  ▼  热路径 (只读 artifact · 按最快优先)
┌──────────────────────────────────────────────────┐
│ 1. Farewell shortcut    ~120ms  规则        不动  │
│ 2. Memory shortcuts     ~50ms   规则        不动  │
│ 3. DirectAnswer         ~200ms  embedding   不动  │
│ 4. Intent router        ~100ms  Groq   改：+l1_id │
│    ├─ L1 Skill match    ~300ms  YAML 解释器 ★新建 │
│    │   (声明式 API 调用·零 LLM)                    │
│    ├─ 内置 Skill (Local) ~500ms 现有 14 个  不动  │
│    ├─ L2 Skill (Tactical)~1-2s  小LLM+工具  ★新建 │
│    └─ Cloud LLM+tool-use ~2-3s  Grok 兜底   不动  │
│                                                    │
│ 每层都写 trace                                     │
└──────────────────────────────────────────────────┘
  │
  ▼  TTS → 用户听到回答
  │
  ▼  冷路径 (异步 · 不阻塞用户)
┌──────────────────────────────────────────────────┐
│ Observer 抽 observation    grok-4.20 ~3.4s  已定  │
│ trace 记录本轮执行信息                      已定  │
│                                                    │
│ 夜批 cold path (~1min/天):                        │
│ ├─ Hotspot 检测                             ★新建 │
│ │   "过去 7 天 Cloud LLM 做的某类请求 ≥ N 次"     │
│ ├─ Skill 编译                               ★新建 │
│ │   trace → LLM → L1 YAML 或 L2 SKILL.md          │
│ └─ Shadow → Live 晋升                      ★新建 │
│     shadow 跑 3 天 → 对比 Cloud 输出               │
│     对齐率 ≥ 阈值 → 升 live                       │
└──────────────────────────────────────────────────┘
```

**3 个决策点（已敲定）**：
1. v1 **全删重构** · 保留有用部分（安全扫描逻辑）
2. 14 内置 skill → **6 个 YAML tool + 5 个 @jarvis_tool 函数 + 7 个砍掉**（Skill class 全废 · ToolRegistry 统一）
3. **自动 + 手动** · 共享编译管道：自动 = 夜批 hotspot 检测 → 编译；手动 = 用户说"学会X" → 跳过 Step 1+2 直接进 Step 3 编译

---

## 十五、全自动学习闭环（trace → skill）

核心问题：**Jarvis 怎么知道"这个事情我应该学会"？** 依据只有 trace。

### 4 步闭环

```
每晚 03:00 cron
  │
  ▼
[Step 1 · 检测] SQL 查 cloud_llm + outcome≥0 的 trace (50-100 条)
  │
  ▼
[Step 2 · 聚类] LLM 聚类："这些请求分几类·每类 ≥ N 次的是 hotspot"
  │              产出：hotspot 清单 [{name, traces[], count}]
  ▼
[Step 3 · 编译] 对每个 hotspot：LLM 编译器判断 L1/L2/SKIP
  │  ├─ L1 → 生成 YAML → skills/learned/<name>.yaml
  │  ├─ L2 → 生成 SKILL.md → skills/learned/<name>.md
  │  └─ SKIP → 太复杂·继续走 Cloud
  │  状态：shadow
  ▼
[Step 4 · 验证] 接下来 3 天·每次该类请求进来：
                同时跑 shadow skill + Cloud LLM · 比较输出
                对齐率 ≥ 80% → live（下次直接走热路径）
                对齐率 < 80% → 延长 shadow / 废弃
```

### Step 1 · 检测（纯 SQL · 零 LLM）

```sql
SELECT user_text, tool_calls, assistant_text, outcome_signal
FROM trace
WHERE path_taken = 'cloud_llm'
  AND outcome_signal >= 0
  AND created_at > now() - 7 days
ORDER BY created_at DESC
```

### Step 2 · 聚类（倾向方案 b · LLM 单次调用）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| (a) Embedding | bge-small-zh → cosine → DBSCAN | 快·便宜·可解释 | 语义远但功能同的可能聚不到 |
| **(b) LLM** | 全喂 LLM："把功能相似的分组" | 语义强·"算汇率"和"一万日元多少钱"一定分到一组 | 每晚一次 <1k token · $0.001 |
| (c) 混合 | embedding 粗筛 → LLM 精分 | 兼顾速度+准确 | 工程复杂一级 |

倾向 (b)：日均 50-100 turn · 7 天过滤后剩 50-100 条 · 不值得为省 $0.001 写 DBSCAN。

### Step 3 · 编译（从 trace 到 skill）

判据：skill 需要 LLM 思考吗？
- **不需要**（纯 API 调用 + 参数映射）→ **L1 YAML**："查汇率" / "开灯"
- **需要**（理解上下文 · 多步推理）→ **L2 tactical**："帮我规划明天行程" / "总结这周会议"

编译 prompt 输入：该 hotspot 的多条 trace（user_text + assistant_text + tool_calls）。LLM 产出 YAML/SKILL.md → 写入 `skills/learned/`。

L1 YAML 示例：
```yaml
name: exchange_rate
trigger_patterns: ["汇率", "换算", "多少钱"]
api: https://open.er-api.com/v6/latest/{base}
params_mapping:
  base: {extract: "源货币", default: "USD"}
  target: {extract: "目标货币", default: "CNY"}
response_template: "1 {base} = {rate} {target}"
```

### Step 3 补充 · 编译前 3-gate 验证

编译器产出 YAML 后、进入 shadow 前，跑 3 个简单检查（~50 行 · 零 LLM）：

1. **Gate 1 · Schema 合法**：pyyaml load 不报错 + 必有字段检查（name/description/parameters/action/response）
2. **Gate 2 · Trace replay**：把原 user_text 喂进新 skill · 看输出是否合理（和历史 assistant_text 对比）
3. **Gate 3 · 去重**：新 skill 和现有 tool 的 embedding 相似度 < 0.9 · 防重复编译

任一 gate 失败 → 不进 shadow · log 原因 · 跳过。

### Step 4 · 验证 + 晋升（Shadow 模式）

- **≥5 次触发 + 7 天** 双条件（日均 50-100 turn · 某个 skill 7 天可能只触发 5 次）
- shadow 期间：用户请求同时走真实路径（Cloud LLM）+ 影子路径（新 skill）· 对比输出 · 用户体验不变
- 对齐评估：前期用 **LLM-as-judge 一层**（$0.003/次 · 一天几毛钱）· 规模起来再加分层
- 对齐率 ≥ 80% → `status: shadow → live`
- 失败率 > 20% → 自动 rollback · 废弃

推后：
- Shadow 3 层评估（结构匹配 → embedding → LLM-as-judge）→ 等 learned skill 多了再加
- Shadow 最低样本数 ≥30 → 用量大了再调高
- Post-promotion 48h canary → 等有 3+ 个 live skill 时再建

### 1 个未定项

1. **L2 tactical 的 SKILL.md 格式**：怎么让"小 LLM + 工具白名单"执行 · 下一步讨论

---

## 十六、L1 YAML 格式定义

### 6 个设计决策（调研结论）

**(a) YAML 最小必要字段**（HA / n8n / MCP / OpenAI 共性）
- `name · description · parameters[{name, type, description, required}]`
- 执行侧：`action (url/method/headers)` + `response (extract + template)`

**(b) 表达式语言：Jinja2**
- Home Assistant 10 年生产验证 · 内置 `ImmutableSandboxedEnvironment`（安全沙箱）
- 支持算术 `{{ amount * rate | round(2) }}` · 过滤器 `{{ value | default(0) | float }}` · 条件 `{% if rain %}带伞{% endif %}`
- Python 原生 · 零额外依赖 · 不自己造表达式语言

**(c) 错误处理：三层叠加**
- **层 1 · 步骤级 retry**（抄 n8n）：max 3 次 · delay 1s · exponential backoff · cap 5s
- **层 2 · LLM 可见错误**（抄 MCP）：API 失败 → 返回人话错误文本给 LLM → LLM 自己决定怎么告诉用户
- **层 3 · 降级到 Cloud LLM**（抄 LATM）：L1 skill 彻底失败 → 自动 fallback 到 Grok 硬答 · 对用户透明只是慢一点

**(d) 安全：简单路径**
- config.yaml 配 `allowed_domains` 白名单 · 执行器发 HTTP 前检查
- 屏蔽私网 IP（127.0.0.0/8 · 10.0.0.0/8 · 192.168.0.0/16）防 SSRF
- 密钥用 env var（`WEATHER_API_KEY` 等）· 不搞 AES vault（本地优先 · 过度）

**(e) Tool 数量上限：≤20 per LLM turn**
- Grok-4.1-fast：150 tools 时准确率 86.7% → 76.7%（-10pp）· GPT 系列 150 tools 完全崩
- 当前 14 内置 + 学到的 · 短期不会超 · 但要有监控

**(f) Hotspot 检测阈值**
- 7 天内同类请求 **≥3 次**（调研验证 3 即可 · 配合成功率过滤噪声）
- 成功率 >80%（outcome_signal ≥ 0 的比例）
- shadow 跑 7 天 · 失败率 >20% 自动 rollback
- batch 处理比 online 好 8.9×（夜批设计印证）

### 2 个翻案

**翻案 1 · L1 不需要自己做 trigger 匹配**
- Intent router 已经在做路由（Groq ~100ms） · 多加一层 regex = 两套并行路由 · 冲突维护成本高
- MCP / OpenAI 的做法：**tool 只负责"是什么 + 怎么执行" · LLM 负责"什么时候调"**
- → 砍掉 `trigger_patterns` 和 `param_extract` · intent router 加 `skill_id` 字段 · 参数由 LLM 或 router 提取

**翻案 2 · Anka DSL 的 +40pp 不可复用**
- 单一作者 · 未发表 · GitHub 找不到 · baseline 和 metric 无法验证
- → 不参考 Anka · 以 HA + MCP 为参考

### 核心设计：一份 YAML 两种消费

同一份 YAML 文件同时服务两种场景：
- **L1 热路径**（无 LLM · ~300ms）：intent router 指定 skill_id + 提取参数 → YAML 解释器执行 action → Jinja2 模板回复
- **Cloud LLM tool-use**（~2s）：YAML 的 parameters 段转成 OpenAI tool definition 给 Grok 当工具调用

底层 action 段共享同一份代码、同一个 API。

### 两种消费方式对比

```
场景 1 · L1 热路径（无 LLM · ~300ms）
  用户: "500 美元换人民币"
    → intent router (100ms): skill_id=exchange_rate, params={amount:500, from:USD, to:CNY}
    → YAML interpreter: action GET API → extract rate → compute → template
    → "500 USD = 3620.00 CNY"

场景 2 · Cloud LLM tool-use（~2s）
  用户: "我出差花了 800 刀机票和 200 欧酒店·总共多少人民币"
    → 太复杂 · intent router 穿透到 Grok
    → Grok 看到 tools 里有 exchange_rate
    → tool_call 1: exchange_rate(800, USD, CNY) → 5792
    → tool_call 2: exchange_rate(200, EUR, CNY) → 1580
    → Grok 自己算+组织回复: "总共 7372 人民币"
```

### YAML Schema（精简版 · 调研后修正）

```yaml
# skills/learned/exchange_rate.yaml

# ===== 元信息 =====
name: exchange_rate
description: "查询实时汇率·货币兑换计算"
version: 1
status: live                       # shadow / live / deprecated
created_at: 2026-04-16T03:00:00Z
created_by: auto                   # auto / manual / migrated
source_traces: [42, 47, 103]       # 哪些 trace 触发了编译

# ===== 参数（同时服务 LLM tool-use 和 L1 执行）=====
parameters:
  - name: amount
    type: number
    description: "金额"
    required: false
    default: 1
  - name: from_currency
    type: string
    description: "源货币 ISO 代码 (USD/EUR/CNY/JPY/CAD...)"
    required: true
  - name: to_currency
    type: string
    description: "目标货币 ISO 代码"
    required: true
    default: CNY

# ===== 安全注解（给 permission_manager 用）=====
annotations:
  read_only: true                  # 不改变任何状态 · 跳过确认
  destructive: false               # 会改变设备/数据状态 · 需确认
  idempotent: true                 # 重复调用无副作用

# ===== 执行 =====
action:
  type: http_get                   # http_get / http_post / python_func
  url: "https://open.er-api.com/v6/latest/{{ from_currency }}"
  headers: {}
  timeout_ms: 3000
  retry:
    max: 3
    delay_ms: 1000
    backoff: exponential

# ===== 结果处理（Jinja2）=====
response:
  extract:
    rate: "{{ result.rates[to_currency] }}"
  compute:
    converted: "{{ amount * rate | round(2) }}"
  template: "{{ amount }} {{ from_currency }} = {{ converted }} {{ to_currency }}"
  error_template: "汇率查询失败·请稍后再试"

# ===== 安全 =====
security:
  allowed_domains: ["open.er-api.com"]
  requires_auth: false
  # auth_env: EXCHANGE_API_KEY    # 需要时取消注释
```

**对比旧版砍掉了什么**：
- ~~`l1.trigger_patterns`~~ — 路由交给 intent router
- ~~`l1.param_extract` + `map`~~ — 参数提取交给 LLM 或 intent router
- ~~自造表达式 `{amount} * {rate}`~~ → Jinja2 `{{ amount * rate | round(2) }}`
- 新增 `retry` 段（n8n 三层错误处理）
- 新增 `error_template`（层 2 MCP 模式 · 给 LLM 看的人话错误）
- 新增 `annotations`（read_only/destructive/idempotent · 给 permission_manager 做安全判断）

### 三层错误处理流程

```
L1 skill 执行
  │
  ├─ action 成功 → response.template → TTS        (正常 · 300ms)
  └─ action 失败
      ├─ [层1] retry 3次 (exponential backoff)
      │   ├─ 某次成功 → 返回                       (抖动恢复 · 3-5s)
      │   └─ 全失败 ↓
      ├─ [层2] 有可读错误？
      │   ├─ 有 → error_template 返回给 Grok       (Grok 翻译错误 · ~2s)
      │   │   → Grok 告诉用户 "这个币种查不到"
      │   └─ 没有 ↓
      └─ [层3] 整个穿透到 Cloud LLM
          → Grok 用训练知识硬答                     (降级兜底 · ~2.5s)
          → 用户无感知 · 只是慢
```

### Skill vs Function vs Tool — 概念重构

```
Skill（旧 v1 遗产）= 独立 Python class + Skill ABC 继承 + SkillRegistry · 14 个文件 14 个 class · 重
Function（新）     = 一个可调用的能力 · 不需要独立文件和 class · 轻
Tool（LLM 视角）   = LLM 看到的 JSON schema {name, description, parameters} · 不在乎背后实现
```

Jarvis 不需要 14 个 Skill class。需要的是**一堆 tool definitions + 对应的 execute 函数**，来源可以混：
- YAML 文件 → 自动生成 tool def + execute 由解释器跑
- Python 函数 → `@jarvis_tool` 装饰器从 type hints 反射 tool def + 直接调用

### ToolRegistry 架构（替代 SkillRegistry）

```
┌──────────────────────────────────────────┐
│          ToolRegistry（统一注册）          │
│                                          │
│  来源 1: skills/*.yaml                   │
│    → YAMLInterpreter 读 YAML → 注册      │
│    → 执行时 Jinja2 解释器跑              │
│                                          │
│  来源 2: tools/*.py                      │
│    → @jarvis_tool 装饰器反射 → 注册       │
│    → 执行时直接调 Python 函数             │
│                                          │
│  来源 3: skills/learned/*.yaml (自动学到) │
│    → 同来源 1                            │
│                                          │
│  对外统一接口：                           │
│    get_tool_definitions() → list[dict]   │
│    execute(name, args) → str             │
│                                          │
│  LLM 看到的：一堆 tools · 不知道背后是啥  │
└──────────────────────────────────────────┘
```

### @jarvis_tool 装饰器（~30 行）

```python
import inspect
from typing import get_type_hints

_TOOL_REGISTRY: dict[str, dict] = {}

def jarvis_tool(func):
    """从函数签名自动生成 tool definition · 注册到全局"""
    hints = get_type_hints(func)
    sig = inspect.signature(func)
    params, required = {}, []
    for name, p in sig.parameters.items():
        ptype = {"str": "string", "int": "integer", "float": "number",
                 "bool": "boolean"}.get(hints.get(name, str).__name__, "string")
        params[name] = {"type": ptype, "description": ""}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    _TOOL_REGISTRY[func.__name__] = {
        "definition": {
            "name": func.__name__,
            "description": func.__doc__ or "",
            "parameters": {"type": "object", "properties": params, "required": required}
        },
        "execute": func,
    }
    return func
```

### YAML 解释器

```python
from jinja2.sandbox import ImmutableSandboxedEnvironment

class YAMLInterpreter:
    """读 YAML · 调 API · Jinja2 渲染回复 · 三层错误处理"""
    env = ImmutableSandboxedEnvironment()

    def execute(self, skill_yaml, params) -> dict: ...
    def render_response(self, skill_yaml, result) -> str: ...
```

### 14 skill 命运

**变成 YAML tool（声明式 · 1 个迁移 + 学到的）**

| Skill | action.type | 说明 |
|---|---|---|
| Weather | http_get | wttr.in · 最典型的 YAML tool |
| (learned) Exchange Rate | http_get | 已有 · skills/learned/ |

**变成 @jarvis_tool 函数（每个 3-10 行 · 不是 class）**

```python
@jarvis_tool
def set_light(room: str, color: str, brightness: int = 100) -> str:
    """控制房间灯光"""
    return device_manager.set_light(room, color, brightness)

@jarvis_tool
def get_time() -> str:
    """获取当前时间日期"""
    return datetime.now().strftime(...)

@jarvis_tool
def add_reminder(text: str, time: str) -> str: ...
@jarvis_tool
def add_todo(text: str) -> str: ...
@jarvis_tool
def get_news(topic: str = "general") -> str: ...
```

| 旧 Skill | 新形态 | 说明 |
|---|---|---|
| SmartHome | @jarvis_tool set_light / get_device_status | Hue API 调用 |
| Time | @jarvis_tool get_time | datetime |
| Reminder | @jarvis_tool add/list/delete_reminder | LLM 解析时间 |
| Todo | @jarvis_tool add/list/check_todo | |
| RealTimeData | @jarvis_tool get_news / get_stocks | 或迁移为 YAML http_get |

**砍掉（概念保留 · 代码删 · 以后需要再加一个函数）**

| 旧 Skill | 理由 |
|---|---|
| MemorySkill | OM 替代 · Observer 自动记 · stable prefix 自动读 · 候选砍掉 |
| Automation | 场景复杂 · 先不迁移 · 看实际用不用 |
| SystemControl | macOS 音量/启动 app · RPi5 上没用 |
| Health | 系统状态查询 · 可选 · 用过再加 |
| Remote | 远程控制 · 用过再加 |
| Scheduler | 和 Reminder 重叠 · 合并或以后重做 |
| SkillManagement | v2 管理界面另起 |

**变成规则（不是 tool）**

| 旧 Skill | 新形态 |
|---|---|
| ModelSwitch | jarvis.py 里的规则 · "仔细想想" → 切 deep 模型 · 不暴露为 LLM tool |

### 最终目录结构

```
~/Projects/jarvis/
├── tools/                           # Python @jarvis_tool 函数
│   ├── __init__.py                  # @jarvis_tool 装饰器 + ToolRegistry
│   ├── smart_home.py                # set_light / get_device_status
│   ├── time_utils.py                # get_time / get_date
│   ├── reminders.py                 # add/list/delete_reminder
│   ├── todos.py                     # add/list/check_todo
│   └── news.py                      # get_news / get_stocks
├── skills/                          # YAML 声明式 tool
│   ├── weather.yaml
│   └── learned/                     # 自动学到的
│       └── exchange_rate.yaml
├── core/
│   ├── tool_registry.py             # 统一注册: 扫 tools/*.py + skills/*.yaml
│   └── yaml_interpreter.py          # 读 YAML · 调 API · Jinja2 · 三层错误处理
```

从 14 个 Skill class 文件 → **5 个轻量 Python 文件 + 1-2 个 YAML**。加新能力 = 加一个 `@jarvis_tool` 函数或一个 `.yaml` 文件。

---

## 十七、工具块完整已确认 + 推后清单

### 已确认

**架构转型**
```
旧：14 个 Skill class（继承 ABC · 每个独立文件 · SkillRegistry 管理）
新：ToolRegistry 统一管理两种来源
    来源 1: YAML 声明式 skill → yaml_interpreter 执行
    来源 2: Python function + @jarvis_tool 装饰器 → 直接调用
    LLM 看到的：统一的 tool definitions · 不知道背后是 YAML 还是 Python
```

**SkillFactory v1 处理**
- 全删重构
- 保留：安全扫描逻辑（15 个危险 pattern 检测）→ 移到编译 gate
- 保留：pytest 验证概念 → 移到 trace replay gate
- 保留：exchange_rate learned skill → 用 YAML 重写
- 删除：skill_factory.py / learning_router.py / 所有 Skill class 文件 / SkillRegistry

**14 旧 skill 命运**

| 类型 | Skill | 新形态 |
|---|---|---|
| YAML | weather | http_get wttr.in |
| YAML (learned) | exchange_rate | 已有 |
| @jarvis_tool | set_light / set_thermostat / get_device_status | 调 DeviceManager |
| @jarvis_tool | get_time | datetime |
| @jarvis_tool | add_reminder / list_reminders | |
| @jarvis_tool | add_todo / list_todos | |
| @jarvis_tool | get_news / get_stocks | |
| 规则 | ModelSwitch | jarvis.py 里的规则 · 不是 tool |
| 砍掉 | MemorySkill | OM 替代 |
| 砍掉 | Automation | 以后用 YAML compose 重做 |
| 砍掉 | SystemControl | RPi5 用不上 |
| 砍掉 | Health / Remote / Scheduler | 以后需要再加 |
| 砍掉 | SkillManagement | v2 管理界面另起 |

**YAML 两种消费方式**
- 方式 1 · L1 热路径（~300ms · 无 LLM）：intent router 输出 skill_id → yaml_interpreter 直接执行
- 方式 2 · Cloud LLM tool-use（~2s）：Grok 看到 tool definition → 自己决定调不调 → 传参 → yaml_interpreter 执行

**表达式语言**：Jinja2（ImmutableSandboxedEnvironment）· Home Assistant 10 年生产验证 · Python 原生

**错误处理（三层）**
- 层 1 · 步骤级 retry（n8n）：max 3 · delay 1s · exponential backoff · cap 5s
- 层 2 · LLM 可见错误（MCP）：error_template 返回给 LLM · LLM 自己告诉用户
- 层 3 · 降级到 Cloud LLM（LATM）：L1 失败 → fallback Grok 硬答 · 用户无感知

**双入口触发机制**
- 入口 1 · 自动（夜批 cron）：SQL 查 → LLM 聚类 (≥3 次=hotspot) → LLM 编译 → 3-gate → shadow → 晋升
- 入口 2 · 手动（实时）：用户说"学会X" → 立即编译 → 3-gate → shadow → 晋升
- 两条路共享：编译器 + 验证 + shadow + 晋升

**3-gate 编译前验证**
1. Gate 1：YAML schema 合法（pyyaml load + 必有字段检查）
2. Gate 2：历史 trace replay ≥80%
3. Gate 3：和现有 skill 去重（embedding 相似度 < 0.9）

**Shadow → Live 晋升**
- 条件：7 天 AND ≥5 次触发
- 比较：前期用 LLM-as-judge 单层（GPT-4o-mini · $0.003/次）
- 对齐率 ≥80% → live · <60% → 废弃 · 60-80% → 延长 shadow

**LLM 池（工具块相关）**

| 用途 | 模型 | 路径 |
|---|---|---|
| 主对话 + tool-use | Grok-4.20-non-reasoning | hot |
| 意图路由 | Groq Llama-3.3-70B（加 skill_id 字段）| hot |
| 夜批聚类 + 编译 | Grok-4.20（<1k token · $0.001/次）| cold |
| Shadow 评审 | GPT-4o-mini（$0.003/次）| cold |
| Fallback | Claude Haiku 4.5 | hot |

**Tool 数量上限**：≤20 per LLM turn · 当前 ~10 Python + 1-2 YAML · 监控 ToolRegistry.count() 超 15 时 log warning

### 推后

| 功能 | 方向 | 触发条件 |
|---|---|---|
| L2 Tactical skill | 小 LLM + 工具白名单 · SKILL.md 格式 | L1 跑稳后 |
| YAML compose | DAG 式多 tool 编排 · 参考 n8n / HA automation | 5+ YAML skill 时 |
| Hotspot 重要性加权 | 用户纠正/重试 = importance×3 | trace 积累 1 个月 |
| Shadow 3 层评估 | 结构匹配 → embedding → LLM-as-judge | learned skill 多了 |
| Post-promotion canary | 48h 监控 trigger rate · 下降 >20% rollback | 3+ live skill 时 |
| 安全加固 | domain 白名单 + 私网 IP 屏蔽 | annotations 已占位 |
| MCP 分发协议 | Zapier 迁移到 MCP · Jarvis 未来扩展点 | 不急 |

### 仍需讨论

**intent router 加 skill_id 后路由准确率**
- Groq Llama-3.3-70B 可能不够聪明从 20 个 skill 里选对
- 不影响设计 — router 选错了 fallback 到 Cloud LLM 兜底
- 上线后看准确率 · 不准就去掉这个字段 · 全走 Cloud LLM tool-use

---

## 十八、Jarvis v2 整体架构

```
                    ┌─────────────────────────┐
                    │   ⑤ 人格层 (常量)        │
                    │   personality.py         │
                    │   小月 / Murasame 模式    │
                    │   包裹每次 LLM 调用       │
                    │   只能 human diff+approve │
                    └────────────┬────────────┘
                                 │
═══════════════════════ HOT PATH (用户感知) ═══════════════════════
                                 │
  用户说话                        │
     │                           │
     ▼                           ▼
  ┌────────┐            ┌──────────────┐
  │③ 感知  │────────────│ ⑥ 路由 + 执行 │
  │        │            │              │
  │ ASR    │            │  按最快优先穿透：
  │ SenseVoice INT8     │              │
  │ ~75ms  │            │  1. Farewell          规则     ~120ms
  │        │            │     "再见/晚安" → 直出
  │ 声纹   │            │
  │ ECAPA  │            │  2. Memory shortcut   规则     ~50ms
  │        │            │     "记住X" → 直存
  │ VAD    │            │
  │ Silero │            │  3. DirectAnswer      embed    ~200ms
  │        │            │     embedding 精确匹配 → 模板回复
  │ 打断   │            │
  │ XVF3800│            │  4. Intent router     Groq     ~100ms
  │ (待到) │            │     Llama-3.3-70B
  └────────┘            │     输出: intent + confidence + skill_id
                        │     │
                        │     ├─ L1 YAML skill           ~300ms
                        │     │  yaml_interpreter 直接执行
                        │     │  无 LLM · Jinja2 模板出回复
                        │     │
                        │     ├─ Python function          ~500ms
                        │     │  @jarvis_tool 装饰器注册
                        │     │  set_light / get_time / add_reminder...
                        │     │
                        │     └─ Cloud LLM + tool-use     ~2-3s
                        │        Grok-4.20 主 / Haiku 4.5 fallback
                        │        看到全部 tool definitions
                        │        自己决定调什么·可多次调用
                        │        ToolRegistry.execute() dispatch
                        │
                        └──────────────┐
                                       │
                                       ▼
                              ┌──────────────┐
                              │ ④ 表达层      │
                              │ TTS 流式输出  │
                              │ MiniMax 主    │
                              │ edge-tts 备   │
                              │ 情感映射      │
                              │ 双线程流式    │
                              └──────┬───────┘
                                     │
                                     ▼
                               用户听到回答
                                     │
═══════════════════════ COLD PATH (后台异步) ═══════════════════════
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
                    ▼                ▼                ▼
           ┌──────────────┐  ┌────────────┐  ┌──────────────┐
           │ ① 记忆 · 记  │  │ ⑦ Trace    │  │ ② 工具 · 学  │
           │              │  │            │  │              │
           │ Observer     │  │ 每轮落一条 │  │ 夜批 cron    │
           │ grok-4.20    │  │ 12 字段    │  │ 03:00        │
           │ ~3.4s 异步   │  │            │  │              │
           │              │  │ user_text  │  │ Step 1       │
           │ 抽 0-N 条    │  │ asst_text  │  │ SQL 查       │
           │ observation  │  │ emotion    │  │ cloud_llm    │
           │ 存 obs 表    │  │ path_taken │  │ 路径的 trace │
           │ 算 embedding │  │ tool_calls │  │              │
           │              │  │ latency    │  │ Step 2       │
           │ fallback:    │  │ outcome    │  │ LLM 聚类     │
           │ gemini-2.5   │  │ ...        │  │ ≥3 次=hotspot│
           │ flash        │  │            │  │              │
           └──────────────┘  └────────────┘  │ Step 3       │
                                             │ LLM 编译     │
                                             │ → YAML skill │
                                             │              │
                                             │ Step 4       │
                                             │ 3-gate 验证  │
                                             │ → shadow     │
                                             │ → 7d + ≥5次  │
                                             │ → live/废弃  │
                                             │              │
                                             │ 手动入口:    │
                                             │ "学会X"      │
                                             │ → 立即编译   │
                                             │ → 共享管道   │
                                             └──────────────┘
                    │                │                │
                    └────────────────┼────────────────┘
                                     │
                                     ▼
              ┌──────────────────────────────────────────┐
              │           数据底座 (SQLite + FastEmbed)    │
              │                                          │
              │  表 observations                          │
              │    id / chunk_id / created_at / content   │
              │    source_turn_id / superseded_by         │
              │    embedding (bge-small-zh · DirectAnswer)│
              │                                          │
              │  表 trace                                 │
              │    id / session_id / turn_id / created_at │
              │    user_text / assistant_text             │
              │    user_emotion / tts_emotion             │
              │    path_taken / tool_calls / llm_model    │
              │    llm_tokens_in / llm_tokens_out         │
              │    latency_ms                             │
              │    outcome_signal / outcome_at_turn_id    │
              │                                          │
              │  文件 skills/*.yaml + skills/learned/*    │
              │    status: shadow / live / deprecated     │
              └──────────────────────────────────────────┘
```

### LLM 池总览

| 用途 | 模型 | 路径 |
|---|---|---|
| 主对话 + tool-use | Grok-4.20-non-reasoning | hot |
| 深度回忆 (100k) | Grok-4.20-non-reasoning | hot |
| 意图路由 | Groq Llama-3.3-70B | hot |
| Observer (记忆抽取) | Grok-4.20-non-reasoning | cold |
| Observer fallback | Gemini 2.5 Flash | cold |
| 夜批聚类 + 编译 | Grok-4.20-non-reasoning | cold |
| Shadow 评审 | GPT-4o-mini | cold |
| Fallback LLM | Claude Haiku 4.5 | hot |

### 数据流向

```
用户说话
  │
  ├──→ trace 表（每轮必写）
  │       │
  │       ├──→ Observer 读 trace → 写 observations 表
  │       │       └──→ 下次热路径: stable prefix 注入 Grok prompt
  │       │
  │       └──→ 夜批读 trace → hotspot → 编译 → YAML skill
  │               └──→ 下次热路径: ToolRegistry 多一个 tool
  │
  └──→ 热路径直接用: DirectAnswer / intent router / tool-use
```

### v1 → v2 变化总结

| 模块 | v1 现状 | v2 目标 |
|---|---|---|
| 记忆·记 | GPT-4o-mini extraction · SQLite + dedup + profile | Observer grok-4.20 · OM 格式 · append-only · emoji priority |
| 记忆·读 | 向量 top-k + GPT 问答 · DirectAnswer 快路径 | stable prefix 全塞 prompt · Grok 自己读 · DirectAnswer 保留 |
| 记忆·推断 | 无 | LLM 读 prefix 自然涌现 · 跨段 = reflection (推后) |
| 工具·用 | 14 Skill class · SkillRegistry | ~10 @jarvis_tool + YAML skills · ToolRegistry |
| 工具·学 | SkillFactory v1 · Claude CLI · 60-80% 成功率 | 双入口自动学习 · LLM 编译 → YAML · 3-gate → shadow → live |
| 路由 | IntentRouter · Groq Llama-3.3-70B | 加 skill_id 字段 · L1 YAML ~300ms |
| 数据底座 | behavior_log · memory SQLite | trace 表 + observations 表 + skills/*.yaml |

### 实施顺序

**Phase 1 · 地基**
1. trace 表 schema migration（behavior_log → trace）
2. observations 表 + Observer 集成（grok-4.20）
3. stable prefix 读取 + 注入 Grok prompt
4. verify：对话后能看到 observation 落库 · Grok 能引用历史

**Phase 2 · 工具重构**
5. @jarvis_tool 装饰器 + ToolRegistry
6. 迁移 5 个 Python function（smart_home / time / reminder / todo / news）
7. weather.yaml + yaml_interpreter
8. 删除旧 Skill class / SkillRegistry / SkillFactory v1
9. verify：现有功能不 break · weather 走 YAML 路径

**Phase 3 · 学习闭环**
10. 夜批 hotspot 检测 + LLM 编译器
11. 3-gate 验证
12. shadow → live 晋升
13. "学会X" 手动入口接入
14. verify：手动说"学会查汇率" → 编译出 YAML → shadow → live

**Phase 4 · 打磨（推后功能按需做）**
15. intent router 加 skill_id
16. reflection（记忆·主动）
17. L2 tactical skill
18. YAML compose
19. 安全加固 / canary monitoring

---

## 十九、能力谱与架构哲学

### Jarvis 七种能力

| # | 能力 | 本质 | 映射到系统 |
|---|---|---|---|
| 1 | 感知 | 知道发生了什么 · 听/看/识别 | ③ 感知块（ASR/VAD/声纹/情感）|
| 2 | 执行 | 按指令做事 · 单步操作 | ② 工具块（YAML tool + @jarvis_tool）|
| 3 | 思考 | 帮你动脑 · 分析/解释/决策 | LLM 本身（无工具 · 纯推理）|
| 4 | 规划 | 把复杂事拆开做完 | LLM tool-use 循环 / L2 tactical |
| 5 | 记忆 | 记住你是谁、你要什么 | ① 记忆块（Observer + stable prefix）|
| 6 | 主动 | 不等你开口 · 预判需求 | cron/事件驱动层（推后）|
| 7 | 陪伴 | 情感连接 · 性格态度 | ⑤ 人格块 + LLM + 记忆 |

tool/function 只覆盖**能力 2 执行**中"单步操作"的部分。Jarvis 七种能力里，工具层只占一种。

### 核心洞察：原子 tool + 聪明 LLM = 高阶能力自然涌现

5 个真实场景分析：

| 场景 | 需要的能力 | 能否用单个 tool 做 |
|---|---|---|
| RPi 显示器显示 4 个 CC 状态 | 跨设备查询 + 数据格式化 + 本地 UI 渲染 | 不能 |
| 概述上一个 CC session 的回答 | 跨设备文件读取 + LLM 总结 + TTS | 不能 |
| RPi 上控制 Mac 打开微信/暂停音乐 | 跨设备远程命令 | 单个 ssh_exec 可以 |
| 语音输入消息发给 Claude Code | ASR + 跨进程/跨设备通信 | 不能 |
| 截图 Mac 屏幕 → 总结邮件 → 优化 → 写入剪贴板 | 跨设备截图 + 视觉模型 + LLM + 跨设备剪贴板 | 不能 |

共性：**多步 + 跨设备 + 中间需要 LLM 思考 + 调多个原子能力**。不需要给每个场景写 skill，需要的是：

1. **足够细粒度的原子 tool** — ssh_exec / screenshot / clipboard_write / read_file / ui_render / vision_analyze ...
2. **足够聪明的 LLM** — 拿着这些 tool 自己拆解任务多步执行（当前 Grok-4.20 已经能做 · tool-use 循环已在 jarvis.py 里）
3. **跨设备通信层** — RPi↔Mac · SSH 免密登录即可

### 跨设备通信：SSH 即可

```bash
# RPi → Mac 通过 SSH 免密登录（公钥认证）
ssh allen@mac.local "osascript -e 'tell app \"WeChat\" to activate'"    # 打开微信
ssh allen@mac.local "osascript -e 'tell app \"Music\" to pause'"        # 暂停音乐
ssh allen@mac.local "screencapture -x /tmp/screen.png" && scp ...       # 截图
ssh allen@mac.local "pbcopy" <<< "优化后的文本"                          # 写剪贴板
```

不需要 MQTT/gRPC/自建 API。SSH 就是现成的、安全的、双向的远程执行通道。已经在用 scp 同步文件，基础设施零成本。SSH 每次建连 ~100-200ms，高频时用 ControlMaster 保持长连接。

### 原子 tool 粒度标准

**LLM 不需要知道实现细节就能正确调用。**

`remote_exec(host, "osascript -e 'tell app \"Music\" to pause'")` — LLM 需要知道 osascript 语法。
`media_control(action="pause")` — LLM 直接调，不需要知道底层。

原则：常用操作包装成具名 tool · `remote_exec` 作为做不到时的 escape hatch 兜底。

### 原子 tool 完整清单（按域分类）

**跨设备通信（RPi ↔ Mac · 全部 SSH 底层 · 当前完全没有的层）**

| Tool | 签名 | 说明 |
|---|---|---|
| remote_exec | (host, command) → str | 兜底 · 在远程机器跑任意 shell · LLM 应优先用具名 tool |
| remote_screenshot | (host) → image_path | 截屏拉回本地 |
| clipboard_read | (host) → str | 远程读剪贴板 |
| clipboard_write | (host, text) → bool | 远程写剪贴板 |
| remote_read_file | (host, path) → str | 远程读文件 |
| remote_write_file | (host, path, content) → bool | 远程写文件 |
| open_app | (host, app_name) → bool | 打开/激活应用 |
| media_control | (host, action) → str | play/pause/next/prev/volume_up/volume_down |

**本地 RPi 操作**

| Tool | 签名 | 说明 |
|---|---|---|
| ui_display | (component, data) → bool | RPi 显示器渲染 · component = status_panel/text/image |
| play_sound | (file_or_url) → bool | 播放本地音频 · 提醒到期/通知音 |
| run_local | (command) → str | 本地 shell 执行 · 与 remote_exec 对称 |

**智能家居（已有 · 重构为 @jarvis_tool）**

| Tool | 签名 | 说明 |
|---|---|---|
| set_light | (room, color, brightness) → str | 调 DeviceManager |
| set_scene | (room, scene_name) → str | 预设场景 |
| get_device_status | (device_id) → str | 查设备状态 |

**时间/提醒/日程**

| Tool | 签名 | 说明 |
|---|---|---|
| get_datetime | () → str | 当前时间日期 |
| set_timer | (duration, label) → str | 计时器 |
| add_reminder | (text, time) → str | 添加提醒 |
| list_reminders | () → str | 列出提醒 |
| add_todo | (text) → str | 添加待办 |
| list_todos | () → str | 列出待办 |

**信息查询（YAML tool 领地 · 未来"学会"的能力大部分落这里）**

| Tool | 类型 | 说明 |
|---|---|---|
| weather | YAML http_get | wttr.in |
| exchange_rate | YAML http_get | 已有 learned |
| stock_price | YAML http_get | 待做 |
| news | YAML http_get | 待做 |

**感知/视觉**

| Tool | 签名 | 说明 |
|---|---|---|
| vision_analyze | (image_path, question) → str | 喂图片给 VLM · 场景 5 的核心 |

**通信**

| Tool | 签名 | 说明 |
|---|---|---|
| send_message | (platform, recipient, text) → bool | imessage/wechat/telegram |

### 统计

| 域 | 数量 | 类型 |
|---|---|---|
| 跨设备 | 8 | @jarvis_tool |
| 本地 RPi | 3 | @jarvis_tool |
| 智能家居 | 3 | @jarvis_tool |
| 时间/提醒 | 6 | @jarvis_tool |
| 信息查询 | 4 | YAML |
| 感知/视觉 | 1 | @jarvis_tool |
| 通信 | 1 | @jarvis_tool |
| **合计** | **26** | 22 function + 4 YAML |

注意：26 > tool 上限 20。但不是每次都全注入 — intent router 可以按 domain 筛选相关 tool 子集。或者分组：跨设备 tool 只在用户提到 Mac/电脑时注入。

### 5 个场景验证

```
场景 1 · 显示 CC 状态
  remote_exec(mac, "ps aux | grep claude") → 解析状态
  ui_display("status_panel", parsed_data)

场景 2 · 概述 CC session 回答
  remote_read_file(mac, "~/.claude/sessions/xxx.json")
  → LLM 自己总结 → TTS 读出

场景 3 · 打开微信 / 暂停音乐
  open_app(mac, "WeChat")
  media_control(mac, "pause")

场景 4 · 语音发消息给 CC
  ASR → 文本 → remote_exec(mac, "echo 'text' | claude ...")

场景 5 · 截图→总结邮件→写剪贴板
  remote_screenshot(mac) → local_path
  vision_analyze(local_path, "summarize this email draft")
  → LLM 改写优化
  clipboard_write(mac, optimized_text)
```

全部可做 · 全部靠 LLM tool-use 循环自己编排 · 不需要提前写 skill。

### Skill 的重新定义

- **低频/多变的复杂任务** → 交给 LLM 现场规划 · 不需要 skill · 原子 tool 够用
- **高频/固定的编排流程** → 值得固化成 skill（如晨间简报 = 天气+日历+提醒+TTS）· 避免每次让 LLM 重新规划浪费 2-3s + token
- "skill" 是**对一组 tool 调用的固化编排** · 不是第三种工具类型

### Skill 按 LLM 介入程度分层

| 类型 | LLM 介入 | 省了什么 | 例子 |
|---|---|---|---|
| 硬编码 skill | 零 LLM · 纯代码管道 | 省规划 2-3s → ~300ms | CC 状态显示 |
| 模板 skill | 固定编排 + 中间嵌 LLM | 省规划 500ms · LLM 内容处理仍 2-3s | 截图→总结→剪贴板 / 概述 CC 回答 |
| 直接 tool-use | 不需要 skill 层 | 太简单 · 一个 tool call | 打开微信 / 暂停音乐 |

模板 skill 不是 YAML 也不是 class — 就是一个函数，写死步骤顺序，中间调 LLM 时传不同 prompt。

### 多步 skill 自动编译（冷进程扩展 · 待做）

当前冷进程：检测单步重复请求 → 编译成 YAML tool。
扩展方向：检测**多步 tool-use 编排**的重复模式 → 固化成模板 skill。

```
当前（单步）：
  "查汇率" ×5 → 编译 exchange_rate.yaml

扩展（多步）：
  "截图→分析→写剪贴板" ×3 → 编译 screenshot_analyze_skill
  trace 里连续 tool_calls 序列相似 = hotspot
```

走同一条管道（检测→聚类→编译→3-gate→shadow→live），只是 Step 2 聚类看的是 tool_calls 序列模式而非单个 user_text。推后到有足够多步 trace 数据时再做。

### 理论支撑（调研结论）

**原子 tool + LLM 编排是已验证范式：**
- **LATM (ICLR 2024)**：GPT-3.5 用 GPT-4 造的工具，6 个任务追平 GPT-4 直接做，成本低 15x · 分发器 95% 准确率
- **SKiC**：组合能力是预训练 LLM 的潜在能力，仅 2 个示例就能解锁近完美系统性泛化
- **"Greedy Is Enough" (2026-01)**：数学证明任何场景只需对数级小子集的完整动作空间 → "小工具集足以完成开放任务"

**10-30 个工具是甜蜜区间**（多份 benchmark 一致）：
- 10 个以内表现好 · 73+ 需检索退化 17-21pp · 计划的 ~26 个在区间内

**CompWoB 警示**：GPT-4 单任务 94% → 组合任务 24.9%。原子精通 ≠ 组合自动成功 → 需要组合级别测试，不能只测单个 tool。这也支持了固定编排 skill 的价值：把 LLM 工作从"规划+执行"缩小到只有"执行"。

### 跨设备安全与实现修正

**SSH 安全**：RPi 被攻破 = SSH key 泄露 = Mac 全开。
- 解法：Mac 上建 `jarvis` 专用用户 + `command=` 白名单网关脚本
- 风险从"full shell access"收窄到"只能跑 osascript/open/say 等白名单命令"
- 20 分钟配置 · 爆炸半径缩小一个量级

**remote_exec 修正**：不应暴露开放 shell → 走 command= 白名单网关 · LLM 传意图 · 网关翻译成具体命令。或拆成具名 tool 不暴露原始 shell。

**截图的坑**：`screencapture` 通过 SSH 在 headless Mac 上不可靠。正确做法：
- Phase 1：VNC framebuffer 抓取 / Hammerspoon 截图端点
- 长期：Mac 上跑 accessibility MCP server · AX API 直接读文本 · 10-50ms vs 截图+VLM 的 1.5-3s · 90% 场景不需要截图

**跨设备通信演进路线**：
- Phase 1（现在）：SSH + osascript + command= 白名单网关
- Phase 2：Hammerspoon HTTP server（结构化控制 · 20-70ms）
- Phase 3：MQTT（已在 stack 里 · 多设备扩展零摩擦）

### 先例项目

| 项目 | 与 Jarvis 的关系 |
|---|---|
| **OpenClaw** (100k stars) | RPi 上跑 · SSH 跨设备控制是一等公民 · MQTT + HA 集成 · 架构最接近 |
| **Extended OpenAI Conversation** (HA 插件) | YAML function-schema 热注入 + composite 链式调用 · 和 YAML tool 设计几乎同构 |
| **GPT-Home** (637 stars) | Philips Hue 原生集成 · LiteLLM + LangGraph tool-loop · 精神兄弟 |

Computer Use 方向（Anthropic/OpenAI/Google）确认：atomic action + LLM planning loop 是通用模式。它们用 click/type/scroll 作为原子，Jarvis 用 set_light/ssh_exec/screenshot — 模式相同，原子不同。MCP 本质就是在标准化这件事。

---

## 二十、记忆块完整已确认 + 待定清单

### 已确认 (可以直接写代码)

**LLM 池**

| 角色 | 模型 | 备注 |
|---|---|---|
| 主对话 / 深度回忆 | Grok-4.20-0309-non-reasoning | |
| Observer (抽取) | Grok-4.20-0309-non-reasoning | bench 实测 F1=0.88 |
| Observer fallback | Gemini 2.5 Flash | 0% halluc 防御 |
| 意图路由 | Groq Llama-3.3-70B | 属路由块 |
| Reflector | 后期再定 | reflection 功能推后 |
| Fallback LLM | Claude Haiku 4.5 | |

**数据层（2 张表）**

```sql
-- 表 observations
id              INTEGER PK
chunk_id        INTEGER          -- Observer 每次产一块
created_at      TIMESTAMP
content         TEXT             -- 整段 markdown · 日期分组 + emoji 行
                                 -- 示例：
                                 -- Date: 2026-04-15
                                 -- * 🔴 (14:30) 用户偏好客厅灯暖黄色 2700K
                                 -- * 🟡 (14:30) 用户语气疲惫·说"累死了"
                                 -- * ✅ (14:30) 客厅灯已调为暖黄
source_turn_id  INTEGER FK→trace
superseded_by   INTEGER          -- 留字段·默认 null·将来纠正/reflection 用

-- 表 trace (原 behavior_log 升级·改名)
id / session_id / turn_id / created_at
user_text / assistant_text
user_emotion    TEXT             -- SenseVoice 识别
tts_emotion     TEXT             -- TTS 用的情感
path_taken      TEXT             -- farewell/direct_answer/l1_skill/local/l2_skill/cloud_llm
tool_calls      JSON             -- (name, args, result, ms)
llm_model / llm_tokens_in / llm_tokens_out
latency_ms      INTEGER          -- end-to-end
outcome_signal  INTEGER          -- null/-1/0/+1
outcome_at_turn_id INTEGER
```

**Observation 格式（抄 Mastra + Jarvis 增补）**

| Emoji | Priority | 含义 | 处理 |
|---|---|---|---|
| 🔴 | HIGH | 身份·偏好·目标·关键事实 | 注入 context |
| 🟡 | MED | 项目细节·工具结果·情感状态 | 存库·注入 context |
| 🟢 | LOW | 不确定·次要 | 存库·注入 context |
| ✅ | DONE | 任务完成信号 | 注入 context |

- 格式：`* <emoji> (HH:MM) <中文陈述句>`
- 按日期分组：`Date: 2026-04-15`
- 每 exchange 1-5 条 · 简洁 · 第三人称
- 注：Mastra 原版读取时只注入 🔴，但我们 bench 测的是全注入。前期 context 小（<25k）全塞不是问题，后期超标时再按 priority 筛

**Observer prompt（已 bench 验证过的版本）**

英文骨架 · 中文产出 · 通过 function call 输出。章节覆盖：
- YOUR JOB + FORMAT RULES
- PRIORITY EMOJI (🔴🟡🟢✅)
- DISTINGUISH ASSERTIONS FROM QUESTIONS
- STATE CHANGES (新状态覆盖旧)
- PRESERVE UNUSUAL PHRASING
- PRECISE VERBS (动词保真·不弱化不强化)
- DETAILS IN ASSISTANT CONTENT (保留具体数值)
- EMOTION DETECTION (SenseVoice 情感 → 🟡)
- AUTHORITY (用户断言为权威)

输出 schema：`record_observations` tool call → `{observations: [{priority, time, text}]}`

**子能力 1 · 记（写入侧·异步）**
- 触发：每轮对话结束（assistant 说完）
- 流程：拉 trace 的 user_text + assistant_text + tool_results → Observer (grok-4.20) function call 抽 0-N 条 → 拼 markdown 段落 INSERT INTO observations → 计算 embedding（DirectAnswer 快路径要用）
- 延迟：异步 · 用户感知 0
- 失败：log warn → 跳过

**子能力 2 · 读（hot path · 拼 prompt）**

触发：每次进 Cloud LLM

```
stable_prefix 结构：
  [personality 系统提示]
  [core profile: 姓名/身份/硬偏好]
  "The following observations are your memory of past conversations..."
  "Newer observations supersede older ones..."
  <observations>
    <chunk N 按时间正序·emoji+timestamp+text>
    --- message boundary (ISO) ---
    <chunk N+1>
    ...
  [最近 10 turn]
  [本 turn user input]
```

- Phase 1：全塞（无筛选 · 前 3 个月 <25k token）
- 何时升级：observations > 25k token（约 100 天后）再按 priority 筛

**子能力 3 · 推断（零代码）**
- 即时推断：LLM 读 stable prefix 时自动发生，只要两条相关 obs 都在 prompt 里
- 跨段推断：走 reflection（后期）

**对外接口**

```python
memory.write_observation(turn_id) → async        # 子能力 1
memory.build_stable_prefix() → str               # 子能力 2
# 以下后期：
memory.run_weekly_reflection() → int             # 子能力 4·cold
memory.feedback(reflection_id, signal) → None    # 子能力 4·feedback
```

---

### 推后 (设计方向已定·不做·等数据积累)

**reflection_generations 表（推后·不建）**
- 独立表 + generation 历史（抄 Mastra 存储模式）
- 不是 in-stream rewrite（Mastra 自己都在重写这部分）
- 新 generation 写新记录 · 老代保留 · 切 active 指针
- 补 Mastra 缺失的：关键事实白名单（过敏/健康/身份/亲属/金额）
- 新 generation diff 白名单 · 缺失则拒绝 swap · 保留 G-1
- token 上限 ≤20k（Mastra 40k 炸了·保守腰斩）
- Reflector 模型：暂定 GPT-4o-mini · 待评估
- 触发：token ≥ 40k 或 距上次 ≥ 7 天（双条件）

**子能力 4 · 主动 (reflection + trigger)**
- `weekly_reflection()`：SELECT 最近 7 天 obs → Reflector LLM → 产 1-5 条高层洞察 → 存 reflection_generations
- 注入方式：独立 section（策略 γ）· 和 obs stream 分开：`── 💭 Active Reflections (基于历史的推断·非硬事实) ──`
- trigger：morning briefing 带出 high priority reflection
- feedback：outcome_signal 驱动 α,β → pass_rate → deprecated

**显式纠正快通道（推后）**
- 用户说"不对/错了" → 不等 Reflector · 当轮 Observer 立即触发
- 写 observation + superseded_by 标记旧条目
- 目前没 Reflector · 没 lag 问题 · 不急

---

## Phase 3 · 全自动学习闭环（trace → skill）

核心问题：Jarvis 怎么知道"这个事情我应该学会"？依据只有 trace。

两个入口：
- **自动**：每晚 03:00 cron 夜批
- **手动**：用户说"学会 X" → 跳过 Step 1+2 直接进 Step 3

### 流程图

```
═══════════════ 入口 A · 自动（每晚 03:00）═══════════════

[Step 1 · 检测] ← 纯 SQL，零 LLM
  │
  │  SELECT user_text, tool_calls, assistant_text, outcome_signal
  │  FROM trace
  │  WHERE path_taken = 'cloud_llm'
  │    AND (outcome_signal IS NULL OR outcome_signal >= 0)
  │    AND created_at > now() - 7 days
  │
  │  产出：50-100 条 cloud 路径的成功 trace
  │
  ▼
[Step 2 · 聚类] ← LLM 单次调用，<1k token，$0.001
  │
  │  把全部 trace 的 user_text 喂给 LLM：
  │  "这些请求按功能分组，每组 ≥3 次的标为 hotspot"
  │
  │  产出：hotspot 清单
  │  [{name: "汇率查询", traces: [42,47,103], count: 7},
  │   {name: "翻译",    traces: [55,61,88],  count: 5}]
  │
  │  过滤：count < 3 → 丢弃
  │       成功率 < 80% → 丢弃
  │
  ▼
═══════════════ 入口 B · 手动 ════════════════════════════

  用户说"学会查汇率"
  │
  │  跳过 Step 1+2
  │  直接构造 hotspot：从 trace 搜相关记录
  │
  ▼
═══════════════ 共享管道 ═════════════════════════════════

[Step 3 · 编译] ← LLM 编译器
  │
  │  输入：该 hotspot 的多条 trace
  │       (user_text + assistant_text + tool_calls)
  │
  │  LLM 判断：这个 skill 需要 LLM 思考吗？
  │    ├─ 不需要（纯 API 调用 + 参数映射）→ L1 YAML
  │    │   例："查汇率" / "查天气" / "开灯"
  │    │   → 生成 skills/learned/<name>.yaml
  │    │
  │    ├─ 需要（理解上下文 · 多步推理）→ L2 tactical
  │    │   例："规划行程" / "总结会议"
  │    │   → 生成 skills/learned/<name>.md  ← 格式未定
  │    │
  │    └─ 太复杂 → SKIP · 继续走 Cloud LLM
  │
  │  状态设为：shadow
  │
  ▼
[3-Gate 验证] ← 零 LLM · ~50 行代码
  │
  │  Gate 1 · Schema 合法
  │    pyyaml load 不报错 + 必有字段检查
  │    (name/description/parameters/action/response)
  │
  │  Gate 2 · Trace Replay
  │    把原 user_text 喂进新 skill
  │    和历史 assistant_text 对比
  │    对齐 ≥ 80%
  │
  │  Gate 3 · 去重
  │    新 skill 和现有 tool 的 embedding 相似度 < 0.9
  │    防重复编译
  │
  │  任一 gate 失败 → 不进 shadow → log 原因 → 跳过
  │
  ▼
[Step 4 · Shadow 验证]
  │
  │  接下来每次该类请求进来：
  │  ┌─────────────────────────────────────┐
  │  │ 用户请求                             │
  │  │   ├─→ 真实路径（Cloud LLM）→ 用户听到 │
  │  │   └─→ 影子路径（shadow skill）→ 静默  │
  │  │        比较两者输出                   │
  │  └─────────────────────────────────────┘
  │
  │  评估：LLM-as-judge（GPT-4o-mini · $0.003/次）
  │
  │  晋升条件：7 天 AND ≥ 5 次触发
  │    对齐 ≥ 80% → status: shadow → live
  │    60-80%     → 延长 shadow
  │    < 60%      → 废弃
  │
  ▼
[Live]
  │
  │  下次同类请求 → 直接走 skill 热路径
  │  不再过 Cloud LLM
  │  延迟从 ~2-3s 降到 ~300ms (L1) 或 ~1-2s (L2)
```

### LLM 池（Phase 3）

- 夜批聚类 + 编译：Grok-4.20-non-reasoning（<1k token/次 · $0.001）
- Shadow 评审：GPT-4o-mini（$0.003/次）

### Phase 3 未定项

L2 tactical 的 SKILL.md 格式。
需要定义：小 LLM 怎么读这个文件 → 知道调哪些 tool → 按什么顺序 → 中间怎么传参。
本质上是一个"固定编排脚本"的声明式格式。

---

## Phase 4 · 打磨（思路级 · 无实现细节）

### 4.1 Intent Router 加 skill_id

- 现在：router 输出 intent type (smart_home/weather/chat/...)
- 目标：router 额外输出 skill_id，直接指定走哪个 tool
- 价值：L1 YAML skill 可跳过 Cloud LLM · router 直接路由
- 风险：Groq Llama-3.3-70B 可能不够聪明从 20+ tool 里选对
- 兜底：选错了 fallback 到 Cloud LLM tool-use · 用户无感只是慢
- 依赖：Phase 3 跑出几个 live skill 后才值得做

### 4.2 Reflection（记忆 · 主动）

- 现在：Observer 每轮抽 observation · 全塞 prompt
- 问题：100 天后 observations 超 25k token · 塞不下
- 目标：weekly_reflection() 压缩历史 observations 为高层洞察

设计方向（已定但未细化）：
- 独立 reflection_generations 表 + 代际历史
- 新 generation 写新记录 · 老代保留 · 切 active 指针
- 白名单 diff（过敏/健康/身份/亲属/金额不能被压缩丢失）
- token 上限 ≤ 20k（Mastra 40k 炸了）
- 触发：token ≥ 40k 或 距上次 ≥ 7 天
- 注入方式：独立 section · 和 obs stream 分开
  "💭 Active Reflections (基于历史的推断·非硬事实)"
- 未定：Reflector 模型选型、prompt 设计、质量评估方法

### 4.3 L2 Tactical Skill

- 现在：只有 L1 YAML（单 API 调用）
- 目标：支持多步编排 skill（LLM + 工具白名单）
- 例："截图→分析→写剪贴板"这种固定编排
- 未定：SKILL.md 格式、执行引擎、编排语言选型

### 4.4 YAML Compose（DAG 编排）

- 参考 n8n / HA automation 的 DAG 式多 tool 编排
- L2 的更高级形态 — 声明式多步编排 · 非 LLM 驱动
- 触发条件：5+ YAML skill 时才值得
- 现在只有 2 个 YAML skill · 远未到需要的时候

### 4.5 安全加固

（待补充）