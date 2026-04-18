# Jarvis 记忆系统重构计划 v0.1

**日期**: 2026-04-14
**作者**: Allen + Claude
**状态**: 草案，待 review
**触发**: bench_llm_v3 实验证明 Grok 4.20-0309-non-reasoning 可做主 LLM
       （TTFT 488ms @ 30k, Recall 3/3 @ 100k, $0.26/100, cache 100%）

---

## 0. 核心决策（已锁）

1. **主 LLM**: `grok-4.20-0309-non-reasoning`，context_budget 起 30k，预期长到 50-80k
2. **记忆范式**: OM pure 模式 —— 全部 observation append-only，不做 top-k RAG 主通道
3. **快路径**: DirectAnswer 保留 2-3 周，靠 Trace 埋点统计命中率决定生死

## 1. 大前提变化

旧架构的所有节俭动作（profile rebuild / digest / top-k 卡 3k / 半衰期）根子
都是"context 贵 + 长 context 质量崩"。Grok 4.20 把这两条都拆了：

    30k 成本 $0.0026/call
    100k recall 3/3 中文完美
    cache 100% 命中，warm 再砍 75%

所以"压缩/筛选/淘汰"从架构必需退化成洁癖，架构设计必须体现这一点。

## 2. 目标架构

### 2.1 三层 context 读路径

    Layer 0: DirectAnswer (50ms, 0 LLM call)
       FastEmbed 召回 top-1 > 阈值 → 直接答
       覆盖"我住哪""喜欢什么咖啡"类高频 factoid
       试用期 2-3 周，触发率低则砍

    Layer 1: OM pure context (Grok 主通道, ~20-50k)
       ├─ 核心档案 (~500 tokens)                  ┐
       ├─ 全部 observation (append-only)          │← stable prefix, 100% cache
       ├─ 最近 10 轮对话 (~3k)                    ← 变化段
       └─ 当前 user turn                          ← 变化段

    Layer 2: 深挖 (未来, 暂不实现)
       Grok 100k 
       只在 Layer 1 答不上时由 LLM 主动触发

### 2.2 写路径

    用户对话
       ↓
    Trace events (每 turn 一条, append-only, outcome 异步回填)
       ↓
    Extraction（主 LLM 顺手做 or 独立 GPT-4o-mini）
       ↓
    observations 表 (OM 格式, append-only, 不 dedup 不评分)
       ↓
    定期 Reflector (撞阈值触发, 重组压缩, 归档原始)

### 2.3 数据表

保留/改造/新增/砍：

    保留并强化:
      memories      → 降级为"核心档案 + DirectAnswer 高频 factoid"
      FastEmbed     → 只服务 DirectAnswer + 未来 Layer 2 深挖
      episodes      → 最近 10-20 轮对话, 本质不变

    新增:
      observations  → OM 格式 append-only 主干
                      schema: id, created_at, meaning_date, emoji, content, archived
      events        → Trace 事件流 (behavior_log 升级)
                      schema: id, parent_id, conv_id, type, source, created_at,
                              payload, outcome, embedding_key

    砍或冷冻:
      profile rebuild   → 砍, 改 deterministic 累积
      digests           → 砍, Grok 不需要我们帮它压
      relations         → 冷冻, 未来真要图谱上 Graphiti
      6 类 category     → 砍到 3 类或直接废 (OM 不分类)
      半衰期            → 冷冻, observation 不衰减

## 3. 待定问题（需要 Allen 拍板）

### Q2: Observation 粒度

    A. 一轮一条（稀疏）
    B. user/assistant/tool 分三条（稠密, 天然缓解 bug3）
    C. 混合: 用户话+持久事实 → observation; 助手话+tool → episodes
    
    Claude 倾向: C
    Allen 意见: 待定

### Q3: Reflector 压缩后原始 observation

    A. 扔掉（OM 原教旨）
    B. 归档打 archived=true, 不进 context, SQLite 保留
    C. 归档 + 仍参与 FastEmbed 索引, Layer 2 可召回
    
    Claude 倾向: B
    Allen 意见: 待定

## 4. 分阶段 Roadmap（无日期，Step 优先级）

### Step 1 · 奠基 —— observations 表 + append-only 写入

没有这个，stable prefix 没东西可 stable。

工作量估计: 中

    1.1  设计 observations schema（等 Q2/Q3 定案）
    1.2  observation_store.py: 纯 append, 不 dedup
    1.3  改 extraction 流程: 输出从 ADD/UPDATE/DELETE
         变成"生成一条或多条 observation 行"
    1.4  双写过渡: memories 旧表继续写, observations 新表并行写
    1.5  单元测试 + system test 1 个 suite 回归

验收: 一轮对话后 observations 表正确增行，格式符合 OM。

### Step 2 · Stable prefix 构建器

Step 1 落地后顺手做，因为 append-only 天然稳定。

工作量估计: 低

    2.1  prompt_builder.py: 拼 "核心档案 + 全部 observation + episodes"
    2.2  验证 Grok cache 命中率（对比 warm/cold token 消耗）
    2.3  改 LLM 调用路径接 prompt_builder

验收: 连续两次相同 query，第二次 cached_tokens > 95% prefix 长度。

### Step 3 · Trace events 表（behavior_log 升级）

解锁后续决策的数据基础。DirectAnswer 试用期的命中率统计依赖它。

工作量估计: 中

    3.1  events schema + 写接口
    3.2  埋点: voice.turn / memory.recall / llm.gen / tool.invoked /
             directanswer.hit / directanswer.miss
    3.3  outcome 回填逻辑（看下一轮用户反应）
    3.4  旧 behavior_log 迁移脚本

验收: 跑一个 session 后 events 表有完整事件链，outcome 字段非空。

### Step 4 · DirectAnswer / FastEmbed 职责切割

工作量估计: 低-中

    4.1  DirectAnswer 只查"核心档案" factoid, 不碰 observation
    4.2  FastEmbed index 范围收窄到核心档案
    4.3  通过 Trace 观察 DirectAnswer 触发率（2-3 周）

验收: 触发率统计仪表板或脚本，数据足够做 go/no-go 决策。

### Step 5 · 核心档案 deterministic 升级机制

工作量估计: 中

    5.1  扫 observation: 30 天内同一事实被提 3+ 次 → 升级到核心档案
    5.2  纯规则, 零 LLM, 避免漂移
    5.3  升级后原 observation 保留（不删）

验收: 给一段包含重复事实的模拟观察流，核心档案正确累积。

### Step 6 · Reflector（防爆, 双阈值触发）

只在观察流膨胀到威胁 context 预算时激活。

工作量估计: 高

    6.1  阈值监测: observation token 总量
    6.2  Reflector 触发（Claude Sonnet 4.6 or Grok 4.20 reasoning）
    6.3  "重组压缩"prompt: preserve ALL important info, 不产生洞察
    6.4  归档策略按 Q3 决定
    6.5  回滚机制: Reflector 输出异常时保留原始

验收: 模拟 10k observation, Reflector 输出 < 5k 且关键事实一条不丢。

### Step 7 · Layer 2 深挖通道（远期）

LLM 主动触发 100k context 或 Haiku fallback。看 Step 1-6 实际使用后再定设计。

### Step 8 · Insight / Reflection（更远期）

输出具体洞察（trend/pattern/contradiction/opportunity），需要数据积累
（至少 3 个月 observation）。先上 trend（低风险），其他后议。

## 5. 风险与回滚点

    风险                          回滚
    ─────────────────────────────────────────────────
    Grok 长 context 实际中文崩    切 Haiku fallback
    observation 增长太快撞预算    提前启动 Step 6
    Reflector 压坏关键事实        Q3 选 B, 原始可恢复
    DirectAnswer 触发率 <20%      按 Q1 共识砍
    Extraction 漂移导致脏数据     双写过渡期可对比 memories 旧表

## 6. Phase 0 Bug 关系

先修 Phase 0 六个 bug 再启动 Step 1。确认过 0 个 bug 能靠记忆重构
自动解决，所以不能等记忆重构。

    Bug                     记忆重构影响
    ──────────────────────────────────────
    1. 颜色 value=0         无关
    2. LLM 幻觉执行         无关
    3. 多轮上下文丢失       Step 1 (粒度 C) 可能缓解
    4. info_query 误判      无关
    5. 情感元认知           无关
    6. 回复超长             换 Grok 可能缓解

## 7. 下一步

Allen 确认：

    [ ] Q2 粒度选择 (A/B/C)
    [ ] Q3 归档策略 (A/B/C)
    [ ] Roadmap 步骤顺序是否认可
    [ ] Phase 0 bugs 状态 (已修? 修几个?)

确认后进入 Step 1 详细设计（observations schema + 写接口）。
