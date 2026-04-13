# Findings: 记忆系统 Deep Research

## LOCOMO Benchmark（黄金标准）
| 系统 | 准确率 | p95延迟 | tokens/对话 |
|---|---|---|---|
| Full-context | 72.9% | 17.12s | ~26,000 |
| Mem0g(图) | 68.4% | 2.59s | ~14,000 |
| Mem0(向量) | 66.9% | 1.44s | ~7,000 |
| Zep | 76.6% | 0.78s | ~600,000 |
| Letta(文件工具) | 74.0% | — | — |
| OpenAI Memory | 52.9% | — | — |

## 小月 vs Mem0 核心差距
| 维度 | 小月 | Mem0 | 改进项 |
|---|---|---|---|
| 操作 | ADD/UPDATE/NONE | ADD/UPDATE/DELETE/NOOP | C6 |
| 去重候选 | top-5 | top-10 | C6 |
| 冲突 | 被动 corrections | 主动每条检查 | C6 |
| 提取 | 自由 JSON | function calling | C5 |
| 评估 | 84 mock tests | LOCOMO + LLM-Judge | C10 |

## bge-small-zh 分布问题
- 原版分数集中 [0.6, 1.0]，cosine > 0.5 无区分度
- v1.5 修复分布，区分度更好
- 来源: HuggingFace, arXiv 2510.05309

## MemoryBank 遗忘公式（AAAI 2024）
```
strength = importance * e^(-lambda * days) * (1 + recall_count * 0.2)
lambda = 0.16 * (1 - importance * 0.8)
```

## 不适用方案
- Neo4j（RPi5 跑不动）→ SQLite 关系表
- Reranker（额外 API）→ 4 信号够用
- Zep 时序图（600K tokens）→ 太重
- ACAN 跨注意力（需训练数据）→ 暂不适用

## 完整来源
31 篇，详见 `.claude/plans/functional-crafting-crab.md` Section G
