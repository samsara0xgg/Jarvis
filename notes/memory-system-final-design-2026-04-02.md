# 小贾记忆系统 — 最终设计 (2026-04-02)

## 第一性原理

小贾的记忆要解决的不是"存数据"，是让她**在对的时候想起对的事**。
像一个真正了解你的人——你说"周末干嘛"，她自然就知道你周六通常跑步、朋友小王这周来、你最近在减肥。

## 设计目标

- 记忆永不丢失（存一切，聪明地选择展示什么）
- 准确召回（多信号检索，不只靠 embedding）
- 主动关怀（pending 机制，小贾会自然地关心你）
- 最小改动集成（路由器不动，只改 LLM prompt 注入）
- RPi5 4GB 可运行（新增 ~140MB RAM）

## 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| Embedding | bge-small-zh-v1.5 via FastEmbed (ONNX) | 中文 C-MTEB 57.82，复用已有 onnxruntime |
| 向量检索 | numpy cosine similarity | <10k 条记忆，暴力搜 ~2ms，不需要专门的向量库 |
| 元数据存储 | SQLite | 标准库，项目已有 |
| 事实提取 | GPT-4o-mini | ~$0.04/月，LLM 决定什么值得记 |

---

## 完整示例：一次对话中记忆如何工作

```
早上 8 点，Allen 说："今天有什么安排？"

小贾的"脑子里"：
┌────────────────────────────────────────────┐
│ Tier 1 用户画像（始终知道的）：               │
│   Allen，程序员，温哥华，养猫小橘              │
│   喜欢拿铁，不喜欢美式，周六跑步               │
│   目前状态：在做智能家居项目                    │
├────────────────────────────────────────────┤
│ Tier 2 近期事件（最近记得的）：               │
│   昨天：Allen 问了 NVDA 股价，涨了 3%         │
│   前天：Allen 说下周一有面试                   │
│   三天前：Allen 说朋友小王周末来               │
├────────────────────────────────────────────┤
│ Tier 3 检索（因为"安排"这个词想起来的）：      │
│   记忆#42：Allen 周六通常去跑步                │
│   记忆#67：小王这周末来温哥华（time_ref匹配）  │
│   记忆#31：Allen 不喜欢周末加班                │
└────────────────────────────────────────────┘

小贾的回复：
"今天周六，小王不是要来吗？另外你通常这时候去跑步。
对了，下周一你有面试，要不要今天准备一下？"
```

---

## 数据模型

### 三张 SQLite 表

```sql
-- 1. 记忆条目（长期，永不删除）
CREATE TABLE memories (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    content     TEXT NOT NULL,        -- "Allen 周六通常去跑步"
    category    TEXT NOT NULL,        -- fact / event / knowledge / preference
    importance  REAL DEFAULT 5.0,     -- 1-10, LLM 提取时评定
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_accessed TEXT,               -- 每次被检索命中就更新
    access_count INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'extracted',  -- explicit / extracted
    time_ref    TEXT,                 -- 引用的时间点 "2026-04-07"
    tags        TEXT,                 -- JSON: ["运动", "习惯", "周末"]
    superseded_by TEXT,               -- 被更新时指向新记忆
    active      INTEGER DEFAULT 1,
    embedding   BLOB                  -- numpy float32 序列化
);

-- 2. 用户画像（Tier 1，始终注入）
CREATE TABLE user_profiles (
    user_id     TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,        -- 结构化 JSON
    updated_at  TEXT NOT NULL
);

-- 3. 对话摘要（Tier 2 数据源）
CREATE TABLE episodes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    summary     TEXT NOT NULL,        -- "聊了股票行情和下周出差的事"
    date        TEXT NOT NULL,
    mood        TEXT,
    topics      TEXT,                 -- JSON: ["股票", "出差"]
    created_at  TEXT NOT NULL
);
```

### 用户画像 JSON 结构

```json
{
  "identity": {
    "name": "Allen",
    "location": "温哥华",
    "occupation": "程序员",
    "traits": ["养猫叫小橘", "Mac 开发"]
  },
  "preferences": {
    "likes": ["拿铁", "跑步", "三体"],
    "dislikes": ["美式咖啡", "加班"]
  },
  "routines": {
    "周六": "通常去跑步",
    "工作日": "9点开始写代码"
  },
  "pending": [
    {"content": "下周一面试", "date": "2026-04-07", "context": "关心结果"}
  ],
  "status": "在做智能家居项目，最近在设计记忆系统"
}
```

---

## Save 管线（对话结束后，异步不阻塞）

```
对话结束
    │
    ▼
1. LLM 提取（一次云端调用，~$0.0002）
   输入：对话全文 + 当前用户画像
   输出：{
     "memories": [每条: content/category/importance/tags/time_ref],
     "profile_update": 更新后的画像JSON 或 null,
     "episode_summary": "聊了面试和周末计划"
   }
    │
    ▼
2. 去重（embedding 对比）
   对每条新记忆：
     生成 embedding
     和现有记忆做余弦相似度
     > 0.85 → UPDATE（旧记忆 superseded，新记忆继承 importance）
     < 0.85 → ADD
    │
    ▼
3. 持久化
   写入 memories / episodes / user_profiles 表
```

## Query 管线（LLM 调用前，同步）

```
用户说了一句话 (text)
    │
    ▼
并行执行：
  ① Tier 1: 加载 user_profile (~1ms)
  ② Tier 2: 加载最近3天 episodes (~1ms)
  ③ Tier 3: embedding 检索 (~50-200ms)
     a. text → embedding (bge-small-zh)
     b. cosine similarity 搜全部 active 记忆
     c. tag 匹配（时间词 → time_ref）
     d. 多信号打分：
        score = 0.4 × cosine
              + 0.25 × recency
              + 0.2 × importance
              + 0.15 × access_freq
     e. 取 top 5，更新 access_count/last_accessed
    │
    ▼
组装 <memory> 注入 prompt (~500 token)
```

### Prompt 注入格式

```xml
<memory>
[关于 Allen]
程序员，住温哥华，养猫小橘。喜欢拿铁，周六跑步。
目前在做智能家居项目。

[最近]
昨天聊了 NVDA 股价。前天说下周一有面试。

[相关]
他周六通常去跑步。
朋友小王这周末来温哥华。
他不喜欢周末加班。

[待关心]
下周一面试 — 到时候问问结果。
</memory>
```

---

## 记忆生命周期

```
新记忆诞生
    │ importance = LLM 评定
    │ access_count = 0
    ▼
活跃期（经常被检索命中）
    │ access_count 递增，last_accessed 更新
    │ → 容易出现在 Tier 3
    ▼
沉淀期（很久没被提及）
    │ recency_score 降低
    │ → 不太会出现在 Tier 3
    │ → 但搜索时还能找到
    ▼
被更新（用户说了新信息）
    │ 旧记忆 active=0, superseded_by=新id
    │ 新记忆继承旧的 importance
    ╳
永远不删除
```

## 主动关怀机制（pending）

```python
# 每次 query 时检查
for item in profile["pending"]:
    if item["date"] <= today:
        # 注入 Tier 1："他的面试应该结束了，问问结果"
        # 小贾下次对话会自然提起
```

区别：
- 提醒：叮！你有一个面试提醒。← 机器
- 关怀：对了，上次面试怎么样了？← 人

---

## 集成方式

```
改的文件：
  core/llm.py          → _personalize_system 接收 memory_context
  core/personality.py   → build_personality_prompt 增加 <memory> 段
  jarvis.py            → handle_utterance 调 memory query/save
  skills/memory_skill.py → 底层换成 MemoryManager

新建的文件：
  memory/manager.py     → MemoryManager (query + save 两个方法)
  memory/store.py       → SQLite 三表操作
  memory/embedder.py    → bge-small-zh via FastEmbed
  memory/retriever.py   → 多信号检索 + 打分

不改的：
  core/intent_router.py, core/local_executor.py, core/tts.py, skills/其他
```

对外接口：

```python
class MemoryManager:
    def query(self, text: str, user_id: str) -> str:
        """返回格式化的记忆文本，注入 prompt"""

    async def save(self, messages: list, user_id: str, session_id: str):
        """后台异步：提取 + 去重 + 存储"""
```

## 性能预算

| 阶段 | Mac | RPi5 | 阻塞用户？ |
|------|-----|------|-----------|
| Query: profile + episodes | ~2ms | ~5ms | 是（极快） |
| Query: embedding | ~50ms | ~200ms | 是 |
| Query: cosine 搜索 | ~2ms | ~5ms | 是 |
| **Query 总计** | **~55ms** | **~210ms** | **可接受** |
| Save: LLM 提取 | ~1.5s | ~1.5s | 否（异步） |
| Save: embedding + 写库 | ~100ms | ~300ms | 否（异步） |

## RAM 预算 (RPi5 4GB)

| 组件 | RAM |
|------|-----|
| OS + 系统 | ~500 MB |
| Python + Jarvis | ~100 MB |
| sherpa-onnx ASR | ~200 MB |
| SpeechBrain 声纹 | ~150 MB |
| TTS | ~100 MB |
| bge-small-zh embedding | ~120 MB |
| 记忆向量 (10k×512d) | ~20 MB |
| **总计** | **~1.2 GB，剩余 ~2.8 GB** |

## 边界情况处理

- **冷启动**：记忆为空时三层都跳过，系统正常运行，几次对话后自动积累
- **记忆矛盾**："住温哥华" → "搬到多伦多" → supersede 模式，旧记忆不删但标记
- **画像膨胀**：画像 JSON 有 ~200 token 预算，LLM 生成时会自动优先级排列
- **敏感信息**：v1 本地存储明文（文件安全等同设备安全），未来可加加密
- **多用户**：所有表按 user_id 隔离，guest 无记忆
- **模型升级**：embedding 存 embedding_version 字段，换模型时后台重新 embed
