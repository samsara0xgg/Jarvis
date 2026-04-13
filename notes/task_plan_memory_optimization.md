# Task Plan: 小月记忆系统优化（基于 Deep Research）

## Goal
基于 Mem0/MemGPT/ChatGPT/Gemini/Zep 等系统的 deep research（31篇来源），对小月记忆系统进行 13 项改进，涵盖 bug 修复、提取质量、检索智能、情景记忆四个阶段。

## Current Phase
ALL COMPLETE ✅ (Phase 1-4 done)

## Phases

### Phase 1: 关键 Bug 修复（~1天）
- [ ] **C1** timezone fix — `store.py:377` 改 `date('now','localtime',?)`
- [ ] **C2** DA 阈值校准 + bge-small-zh-v1.5 升级 — 校准脚本 + embedder 升级 + migration
- [ ] **C3** 过期记忆自动清扫 — maintain() 加 `_sweep_expired()`
- [ ] **C4** 注入预算 1200→2000 + 使用原则外移到 personality.py
- [ ] 跑 `python -m pytest tests/ -q` 验证
- **Status:** complete ✅
- **结果**: 754 passed (+12 新测试); DA recall 70%; cosine分布正常(已是v1.5)

### Phase 2: 提取质量 — Mem0 启发（~2-3天）
- [ ] **C5** 提取改 function calling + 后处理校验 [M5][M6]
- [ ] **C6** 去重增加 DELETE + top-10 候选 [M1][M2][M3]
- [ ] **C7** 去重阈值实测校准（calibrate_dedup.py）
- [ ] 跑 `python -m pytest tests/ -q` 验证
- **Status:** complete ✅
- **结果**: 767 passed (+13); FC提取+fallback; DELETE操作; top-10; 阈值0.55→0.65(校准)

### Phase 3: 检索智能 + 评估框架（~2天）
- [ ] **C8** 消除 100 记忆硬切换 → 渐进式检索
- [ ] **C9** 检索权重数据驱动调优
- [ ] **C10** 构建评估框架 eval_memory.py [M4]
- [ ] 跑评估建立基线
- **Status:** complete ✅
- **结果**: 785 passed; 硬切换100→20; 冷启动cosine 0.60; eval基线 MRR@5=1.00, DA=5%

### Phase 4: 情景 + 关系增强（~2天）
- [ ] **C11** Episode 去重 + 层级压缩（episode_digests 表）
- [ ] **C12** 情绪信号传导（ASR → memory save）
- [ ] **C13** 简化版关系索引（memory_relations 表，替代 Neo4j）
- [ ] 跑 `python -m pytest tests/ -q` 验证
- **Status:** complete ✅
- **结果**: 806 passed; episode dedup+digest; emotion passthrough; relation index 3 patterns

## Key Questions
1. bge-small-zh 当前版本的 cosine 分布是否确认集中在 [0.6, 1.0]？→ C2 校准脚本回答
2. function calling 提取的 key 缺失率能降到多少？→ C5 后验证
3. 现有 84 个测试中有多少需要因 embedder 升级而更新？→ C2 执行时确认
4. Phase 1-2 完成后，eval_memory.py 的基线准确率是多少？→ C10 建立基线

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Phase 1-2 可并行 | 无依赖，各改不同文件区域 |
| SQLite 关系表替代 Neo4j | RPi5 4GB 跑不动 Neo4j |
| function calling 替代 few-shot | Mem0 论文数据表明结构化输出更可靠 |
| 升级 bge-small-zh-v1.5 | 学术确认原版分布异常，调阈值治标不治本 |
| 自建中文评估集 | LOCOMO 是英文，小月需要中文评估 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| Exa MCP 401 | 1 | `--api-key` 无效，需用 `-e EXA_API_KEY=` 环境变量 |

## Notes
- 完整研究: `.claude/plans/functional-crafting-crab.md`（31 篇来源）
- 每项改动后跑 `python -m pytest tests/ -q`（742 tests）
- commit 可以做，push 必须等用户要求
- personality.py prompt 只有用户允许才能改
