# Mastra Observational Memory — 生产环境规模证据

*生成日期: 2026-04-15 · 数据源: GitHub issues + 社区 + Mastra 官方披露 · 综合置信度: 高（证据充分且互相印证）*

---

## 时间线关键事实

- **Mastra OM 公开发布**: 2026-02-09 (`@mastra/memory@1.1.0`) — 距今约 2 个月
- **Mastra 平台 GA**: 2026-04-09 (6 天前，与 Series A 同步宣布)
- **公司阶段**: YC W25 · 种子轮 $13M (2025-10) + A 轮 $22M (2026-04, Spark Capital) · 团队 8 人
- **Tyler Barnes (Mastra 创始工程师) 自述**: 从 2025-10 开始在自定义 coding agent harness 里 dogfood OM ≈ 4 个月（单人使用）

这个时间线本身就决定了："3/6/12 个月生产数据" 在公开渠道 **不可能存在**。

---

## 五问五答

### Q1. observation stream 跑过 3/6/12 个月的用户报告？

**无公开报告。** 综合三路调研：

- 最长可追溯的真实世界线程：[issue #14110](https://github.com/mastra-ai/mastra/issues/14110) — "over multiple days", 17 代 reflection in ≈2 天
- Mastra 自家最长披露：Tyler Barnes ≈ 4 个月单人 dogfooding（`mastra.ai/research/observational-memory`）
- Showcase 客户（Sanity/Factorial/Index/Replit/PLAID）**没有一家披露使用 OM**，只提 Mastra agents
- Reddit/Twitter/HN 上**零**条独立开发者的多月使用报告

### Q2. Reflector 在特定规模下出错 / 丢数据 / 退化？

**有 — 确认、近期、严重**。4 条高质量 issue 互相印证同一类失败模式：

| Issue | 日期 | 故障 | 状态 |
|---|---|---|---|
| [#14110](https://github.com/mastra-ai/mastra/issues/14110) | 2026-03-10 | Reflector 在 ≈40-43k observation tokens 时陷入无限循环，17 代生成挤在 ≈2 天内（12-16 代几分钟内连续触发），"Agent becomes unresponsive" | 已关闭 |
| [#15062](https://github.com/mastra-ai/mastra/issues/15062) | 2026-04-04 | MAX_COMPRESSION_LEVEL 仍无法压到阈值下时，无限重试而非接受当前最佳结果 | **Open** |
| [#13389](https://github.com/mastra-ai/mastra/issues/13389) | — | 升级 OM 1.5.0 后生产 OOM 崩溃整个周末；紧急 `om-oom` 快照修复 | 已关闭 |
| [#14737](https://github.com/mastra-ai/mastra/issues/14737) | — | 切换模型时 "Buffering observation failed: this request would exceed your limits" | — |
| [#14926](https://github.com/mastra-ai/mastra/issues/14926) | — | OM 在多步 tool-calling 轮次丢失 assistant 文本 | — |
| [PR #14344](https://github.com/mastra-ai/mastra/pull/14344) | 2026-03-17 | 超大 tool result（web-search `encryptedContent`）吹爆 Observer prompt，需 sanitize | 已合入 |

Tyler Barnes 在 #14110 的回复：*"it should be re-prompting with stronger compression guidance by 2 levels and then stop... This sounds like a bug if that's not happening"* — 内置 circuit breaker 存在但失效。

### Q3. Level 3-4 压缩丢失重要信息？

**无直接用户投诉，但维护者自己承认有损且正在重写。**

- [HN #46992444](https://news.ycombinator.com/item?id=46992444) 第三方理论批评：*"compression is inherently lossy and can drop smaller details that might become important later. The Reflector seems to validate success primarily via token thresholds, rather than checking whether rewritten memory remains semantically faithful… over time, this could allow memory drift."*
- #15062 用户变相抱怨："priority-based injection budget (newest + 🔴 items first) so OM stays useful even when the full sheet is large"
- #14110 评论 rickross："there probably needs to be a way to shed older, lower-priority observations when compression can't make room. There's no fixed threshold that won't eventually be exhausted in a long-running session"
- **Tyler Barnes 本人承认**：*"our new upcoming reflection mode… will solve this in a much cleaner and more 'lossless' way"* — 等于间接承认当前压缩是有损的
- 官方只披露聚合压缩比：文本 3-6×（LongMemEval 约 6×），tool 输出 5-40× —— **从未枚举各等级丢失什么类型信息**

### Q4. 性能退化报告（查询延迟 vs observation 规模）？

**有 — 多处确认。虽然这些是更广义的 Memory/semantic-recall 子系统，不是 OM 本身，但说明 Mastra 在规模-延迟曲线上未 production-proven。**

| Issue | 数据点 |
|---|---|
| [#11702](https://github.com/mastra-ai/mastra/issues/11702) | User A (7.4k msgs) 说 "Hi" 带 semantic recall: **30s**；不带: 3s。User B (4.7k msgs): 9s vs 3s。线性退化。部分归因于跨区网络 (us-east-1 vs us-central-1)，PR #14022 在 2026-03-10 修复 |
| [#11150](https://github.com/mastra-ai/mastra/issues/11150) | `@mastra/pg` 在 1M 行 `mastra_messages` 表 + 2k 消息线程上 `ROW_NUMBER() OVER (ORDER BY "createdAt" DESC)` 导致 **5-10 分钟**查询 |
| [#13952](https://github.com/mastra-ai/mastra/issues/13952) | 第 2 条消息时 semantic recall 15s 延迟；68k 向量 / 400 MB 在 Upstash |
| [#11168](https://github.com/mastra-ai/mastra/issues/11168) | semantic recall 拉回大 tool result 超过 context window；memory processors 对 recalled 消息静默不生效 (#9892) |

**官方 benchmark 只给准确率（LongMemEval 94.87% gpt-5-mini），不发延迟曲线、不发 p50/p95、不发 Reflector 墙钟时间。**

### Q5. 最大已知 observation stream 规模？

**≈ 40-43k observation tokens · 17 代 reflection · 2 天** ([#14110](https://github.com/mastra-ai/mastra/issues/14110)) —— 而且这是**崩溃案例**，不是健康稳态。

对照：
- pre-OM semantic recall 用户上限：7.4k 消息 (#11702)、68k 向量 / 400 MB (#13952)
- Mastra 官方 LongMemEval 用例：平均 ≈30k tokens 上下文（静态 benchmark 数据集）
- **无任何公开案例显示 OM 稳定运行到 100k+ tokens 或数周/数月**

---

## 综合成熟度评估

| 维度 | 评级 | 依据 |
|---|---|---|
| GitHub 证据成熟度 | **LOW** | OM 仅 2 个月大，高严重度 bug 仍 open，含无限循环、OOM、丢消息；维护者承认压缩模式要重写 |
| 社区采用信号 | **LOW** | Reddit / Twitter / HN 零独立多月用例；r/LocalLLaMA 的 memory 讨论贴完全不提 Mastra；唯一第三方深度评论 (HN #46992444) 是怀疑论 |
| 官方披露透明度 | **LOW-MEDIUM** | 发了 research post + 开源代码 + LongMemEval 准确率（这点比平均 vendor 好）；但**零**生产运行数据：无部署时长、无延迟曲线、无规模上限、无事故复盘、无丢失信息分类 |

**综合：LOW。**

---

## 对 Jarvis 的结论

1. **Mastra OM 未被任何公开案例验证可稳定运行数月** —— 它太新（2 个月）、Mastra 自己也才 GA 6 天。
2. **确认存在两类硬性规模墙**：
   - Reflector 在 ≈40k tokens 时可能无限循环（2026-04-04 仍 open: #15062）
   - 当前压缩是 Mastra 自己承认的"非 lossless"，upcoming reflection mode 会替换
3. **Jarvis 必须自己设规模兜底**，不能假设 OM 能自然扛过去：
   - 硬限制单线程 observation token 数（参考 #14110 的 40k 红线，留 2× 余量即 ≤ 20k）
   - 自己实现 priority-based shedding（#15062 用户提出的方案，Mastra 还没有）
   - 自己打延迟监控曲线（Mastra 不发 p50/p95，你也不能指望参考值）
4. **借架构思想，别借架构假设**：Observer/Reflector 的分工是好设计，但生产稳定性证据完全缺席。Jarvis 在语音助手场景（毫秒级敏感、长期运行、无损可用性要求）比 Mastra 的 coding agent 场景要求更高 —— 更加没有"抄作业"的余地。

---

## 主要参考来源

### Mastra 官方
- https://mastra.ai/research/observational-memory — 研究帖（含 LongMemEval 准确率 + 压缩比，无延迟数据）
- https://mastra.ai/docs/memory/observational-memory — OM 文档
- https://mastra.ai/blog/series-a · https://mastra.ai/blog/seed-round — 融资公告
- https://mastra.ai/showcase — 客户列表（无 OM 指标）

### GitHub (mastra-ai/mastra)
- #14110 — Reflector 无限循环（40k token 硬墙）
- #15062 — MAX_COMPRESSION 仍重试（**Open**, 2026-04-04）
- #13389 — OM 1.5.0 生产 OOM
- #14737 — buffering observation failed
- #14926 — 多步 tool-call 丢 assistant 文本
- #11702 — semantic recall 30s 延迟 (7.4k msgs)
- #11150 — pg 5-10min 查询 (1M rows)
- #13952 — 15s 延迟 / 68k 向量
- #11168 — processors 对 recalled 不生效
- PR #14344 — web-search encryptedContent 炸 prompt

### 社区
- https://news.ycombinator.com/item?id=46992444 — 唯一第三方深度批评
- r/LocalLLaMA memory 相关贴（1raqi5w / 1np8eda / 1lmni3q）—— 不提 Mastra

---

*生产成熟度判定：**LOW** · 建议：借架构、别抄假设、自设规模兜底*
