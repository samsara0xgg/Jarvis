# 记忆系统完整逻辑链

> 2026-04-03 代码核实后整理

## 总架构（6个组件，3个阶段）

```
组件:
├─ MemoryManager    — 总接口 (query + save)           memory/manager.py
├─ MemoryStore      — SQLite 持久化 (4张表)            memory/store.py
├─ Embedder         — 向量编码 (bge-small-zh, 512维)   memory/embedder.py
├─ MemoryRetriever  — 多信号检索                       memory/retriever.py
├─ DirectAnswerer   — L1 无LLM直答 (cosine > 0.85)    memory/direct_answer.py
├─ ConversationStore — 对话历史 (JSON, 滑动窗口20轮)   memory/conversation.py
└─ MemorySkill      — LLM tool: recall / forget       skills/memory_skill.py

存储 (data/memory/jarvis_memory.db):
├─ memories 表      — 记忆条目 (embedding + 元数据)
├─ user_profiles 表 — 用户画像 JSON
├─ episodes 表      — 对话摘要
└─ behavior_log 表  — 行为日志 (追加型)
```

## 阶段一：读取（每次用户说话时触发）

```
用户说话: "我最喜欢吃什么"
         │
         ├─→ [4] memory_manager.query(text, user_id)
         │       │
         │       ├─ store.get_profile(user_id)     → 用户画像
         │       ├─ store.get_recent_episodes(3天)   → 近期对话摘要
         │       ├─ store.count_active(user_id)
         │       │
         │       ├─ 记忆数 < 100?
         │       │   ├─ YES → store.get_active_memories()  全量注入
         │       │   └─ NO  → embedder.encode(text)
         │       │           → retriever.retrieve(emb, top_k=5)  向量检索
         │       │
         │       └─ _format_memory_context() → "<memory>...</memory>"
         │           ├─ [关于用户] profile → 自然语言
         │           ├─ [最近] episodes (≤5条)
         │           ├─ [记忆] memories (按 char budget 1200)
         │           ├─ [待关心] profile.pending (已到期的)
         │           └─ [使用原则] "像朋友一样自然运用..."
         │
         ├─→ [4b] direct_answerer.try_answer(text, user_id)
         │        │
         │        ├─ 只查 preference / identity / knowledge 三类
         │        ├─ embedder.encode(query)
         │        ├─ candidates_embeddings @ query_emb → cosine scores
         │        ├─ best_score >= 0.85?
         │        │   ├─ YES → "你跟我说过，{content}" → TTS → 结束（跳过LLM）
         │        │   └─ NO  → return None → 继续正常流程
         │        └─ store.touch_memory(id)  更新访问计数
         │
         └─→ memory_context 注入 LLM system prompt
              │
              personality.py: build_personality_prompt(memory_context=...)
              │
              system prompt = <personality> + <output_rules> + <memory> + <situation>
```

## 阶段二：写入（对话结束后异步触发）

```
对话完成 → _executor.submit(memory_manager.save, messages, user_id, session_id)
           │
           ├─ [1] _messages_to_text(messages)
           │       "用户：记住，我喜欢拿铁\n小贾：好的，记住了。"
           │
           ├─ [2] _call_llm_extract()  ← GPT-4o-mini
           │       │
           │       prompt = 提取指令 + 用户名 + 当前画像 + 已有记忆 + 对话内容
           │       │
           │       ↓ LLM返回 JSON:
           │       {
           │         "memories": [
           │           {"content": "用户 喜欢拿铁",
           │            "category": "preference",
           │            "key": "favorite_drink",
           │            "importance": 7,
           │            "tags": ["饮品"],
           │            "time_ref": null,
           │            "expires": null}
           │         ],
           │         "corrections": [],
           │         "profile_update": {"preferences": {"likes": ["拿铁"]}},
           │         "episode_summary": "用户分享了饮品偏好",
           │         "mood": "neutral",
           │         "topics": ["饮品"]
           │       }
           │
           ├─ [3] 处理 corrections（纠正旧记忆）
           │       for correction in corrections:
           │           store.deactivate_memory(user_id, old_content)
           │
           ├─ [4] 每条新记忆 → 去重管线:
           │       │
           │       ├─ embedder.encode(content) → 512维向量
           │       │
           │       ├─ 有 key 且非 event？
           │       │   ├─ YES → store.find_by_key(user_id, category, key)
           │       │   │        ├─ 找到 → 新增 + supersede旧的（确定性更新）
           │       │   │        └─ 没找到 → 走向量去重
           │       │   └─ NO → 走向量去重
           │       │
           │       └─ 向量去重:
           │           ├─ retriever.find_similar(emb, top_k=5)
           │           ├─ top_score vs 阈值:
           │           │   ├─ 同类 < 0.55 或 跨类 < 0.70 → 直接 ADD
           │           │   └─ >= 阈值 → _call_llm_dedup()  ← GPT-4o-mini
           │           │       ├─ ADD → 新增
           │           │       ├─ UPDATE → 新增 + supersede旧的
           │           │       └─ NONE → 跳过（已存在）
           │           └─ store.add_memory(... embedding=emb)
           │
           ├─ [5] 更新用户画像:
           │       ├─ LLM 给了 profile_update？→ store.set_profile()
           │       └─ 没给但有 identity/preference/relationship？→ _rebuild_profile()
           │           ├─ 遍历所有 active memories
           │           ├─ identity → profile.identity[key] = content
           │           ├─ preference → profile.preferences.likes/dislikes
           │           ├─ relationship → profile.relationships[key] = content
           │           ├─ task → profile.pending[{content, date}]
           │           └─ store.set_profile()
           │
           └─ [6] 存对话摘要:
                   store.add_episode(summary, mood, topics)
```

## 阶段三：维护（定期后台运行）

```
scheduler → memory_manager.maintain_all()
             │
             for each user_id:
             ├─ store.get_embedding_index(user_id) → 全量向量矩阵
             ├─ 全对余弦相似度: sim_matrix = embeddings @ embeddings.T
             ├─ 找同类且 cosine >= 0.80 的对 (cap at 10 pairs)
             ├─ for each pair:
             │   └─ _call_llm_merge()  ← GPT-4o-mini
             │       ├─ MERGE → store.supersede_memory(remove, keep)
             │       └─ KEEP_BOTH → skip
             └─ return {merged, checked, skipped}
```

## MemoryRetriever 评分公式

```
score = 0.40 × cosine(query_emb, mem_emb)
      + 0.25 × 1/(1 + days_since_last_access)
      + 0.20 × (importance/10) × decay × reinforcement
      + 0.15 × min(access_count, 10) / 10
      × expiry_factor

其中:
  decay = 1 / (1 + staleness_days / half_life)
  half_life: identity/knowledge=365天, preference/relationship=180天, event=30天, task=14天
  reinforcement = min(1 + 0.05 × access_count, 2.0)
  expiry_factor = 0.5 if expired else 1.0
```

## "记住/记下" 捷径（跳过LLM路由）

```
用户: "记住，我明天要开会"
  ├─ jarvis.py 检测 "记住" 前缀 且不含 "每次"
  ├─ 立即回复 "好的，记住了。"  ← 不走意图路由、不走LLM
  ├─ 存入对话历史
  └─ 后台: memory_manager.save(history) → LLM 提取 → 存库
```

## MemorySkill（LLM tool calling）

```
用户: "我之前跟你说过什么"
  → 意图路由 → complex → 云端LLM
  → LLM 调 recall tool → MemorySkill.execute("recall", {})
      → store.get_active_memories(user_id)
      → 返回所有记忆列表
  → LLM 组织语言回复
```

## 数据流总图

```
                   ┌──────────────┐
                   │   用户说话    │
                   └──────┬───────┘
                          │
              ┌───────────┼───────────┐
              ↓           ↓           ↓
        ┌──────────┐ ┌─────────┐ ┌────────────┐
        │ L1直答   │ │ 记忆查询 │ │ 记忆捷径    │
        │(cosine   │ │→注入LLM │ │("记住"前缀) │
        │ >0.85)   │ │ prompt  │ │快速确认     │
        └────┬─────┘ └────┬────┘ └─────┬──────┘
             │            │            │
             ↓            ↓            ↓
         直接回复    云端LLM处理     口头确认
                     (带记忆上下文)
                          │
                          ↓
              ┌───────────────────────┐
              │   对话完成后 (异步)    │
              │                       │
              │  GPT-4o-mini 提取     │
              │  ├─ memories[]        │
              │  ├─ corrections[]     │
              │  ├─ profile_update    │
              │  └─ episode_summary   │
              │                       │
              │  去重: key匹配→向量→LLM │
              │  存入 SQLite          │
              └───────────────────────┘
                          │
                   ┌──────┴──────┐
                   │  定期维护    │
                   │ 合并相似记忆 │
                   └─────────────┘
```

## 当前状态 (2026-04-03 审计)

已工作:
- SQLite 存储 (4张表, WAL模式)
- LLM 提取 (8条记忆, 14个episode, 207条行为日志)
- 向量嵌入 (512维, 全部8条都有embedding)
- 对话历史 (40轮, default_user)
- 记忆注入 LLM prompt (<memory> block)
- 记忆捷径 ("记住" 前缀快速确认)

待改进:
- L1 直答从未命中 (access_count 全为0, 阈值0.85可能太高)
- Profile 数据错乱 (preferences.likes: ["next Monday"])
- 记忆 key 大量缺失 (8条中4条 key=None)
- expires 基本没设 (只有1条设了过期)
- 全部记忆属于 default_user (未做声纹注册)
- 维护任务未定期运行
