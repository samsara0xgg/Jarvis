# Progress: 记忆系统优化

## Session 1 — 2026-04-04/05

### Deep Research 完成
- 5 个并行研究 agent: Mem0/MemGPT/ChatGPT+Gemini/学术论文/工程实践
- 配置 Exa + Firecrawl MCP
- 综合分析写入 `.claude/plans/functional-crafting-crab.md`
- 31 篇来源

### 关键发现
1. bge-small-zh 原版异常分布 → 升级 v1.5
2. Mem0 DELETE 操作是核心差距 → C6
3. Letta: 简单工具 > 复杂架构 → 验证小贾方向
4. function calling > JSON → C5

### 方案确定
- 4 Phase, 13 项 (C1-C13)
- task_plan.md / findings.md / progress.md 创建

---
## Session 2 — 2026-04-05
### Phase 1 完成 ✅ (754 passed, +12 tests)
- C1: `date('now','localtime',?)` 
- C2: 阈值 0.75→0.55, margin 0.08, embedder 已是 v1.5
- C3: sweep_expired + backfill_expires + pending cleanup
- C4: 预算 1200→2000, 使用原则→personality.py
- Review: 删死代码, maintain()返回值补全
### Next: Phase 2 (C5+C6+C7)
