# AI 系统 Shadow Testing / Canary Deployment 调研
*Generated: 2026-04-15 | Sources: 12 | Confidence: High*

## Executive Summary

业界对 AI/ML 系统的渐进部署已有成熟实践，标准流程是 **Shadow → Canary → A/B → Full**。Uber 已对 75%+ 关键模型启用 shadow deployment。对于我们"新 skill shadow 3 天再晋升"的方案，**三层评估架构**最具性价比：结构化字段精确匹配 → embedding 相似度粗筛 → LLM-as-judge 仅处理灰区。预计成本可控在 **<$0.01/次评估**。

---

## 一、业界实践总览

### 1.1 标准渐进部署流程

业界已形成共识的四阶段模式（[tianpan.co 2026-04](https://tianpan.co/blog/2026-04-09-llm-gradual-rollout-shadow-canary-ab-testing), [MarkTechPost 2026-03](https://www.marktechpost.com/2026/03/21/safely-deploying-ml-models-to-production-four-controlled-strategies-a-b-canary-interleaved-shadow-testing/)）：

| 阶段 | 做什么 | 用户影响 | 持续时间 |
|------|--------|---------|---------|
| **Shadow** | 候选模型并行运行，输出仅记录不返回 | 零 | 3-7 天 |
| **Canary** | 1-5% 真实流量路由到候选 | 极小 | 24-48h per step |
| **A/B** | 50/50 分流，收集统计显著性数据 | 中 | 1-2 周 |
| **Full** | 100% 切换 | 完全替换 | - |

> Shadow mode has real costs. You're running two models simultaneously, which roughly doubles your inference spend during the testing period. — tianpan.co

### 1.2 Uber Michelangelo（最详细的工业实践）

Uber 的 ML 平台 Michelangelo 是目前**公开最详细的 shadow deployment 工程实践**（[Uber Blog 2025-10](https://www.uber.com/blog/raising-the-bar-on-ml-model-deployment-safety/)）：

**规模**：400+ active use cases，20,000+ 月训练任务，峰值 1500 万预测/秒

**Shadow 部署两种模式**：
1. **Endpoint shadowing** — 团队自定义流量分割、验证逻辑、指标。灵活但需更多配置
2. **Deployment shadow** — 全自动，默认运行。目前聚焦 prediction drift detection

**覆盖率**：75%+ 关键模型已启用 shadow（2025 H2 计划 100%）

**安全评分系统**（4 指标）：
- Offline evaluation coverage
- Shadow deployment coverage
- Unit test coverage
- Performance monitoring coverage

**关键 insight**："Many severe incidents begin with upstream data changes — unexpected nulls, distribution shifts, or schema drift." 这和我们的场景类似：新 skill 的输出格式可能和 Cloud LLM 有微妙差异。

### 1.3 Google Duplex

Google Duplex 没有公开 shadow testing 具体流程，但从公开信息可以推断其部署策略（[Google Research Blog 2018](https://research.google/blog/google-duplex-an-ai-system-for-accomplishing-real-world-tasks-over-the-phone/)）：

- **Real-time supervised training**：操作员实时监控系统通话，必要时介入。类似 shadow mode 的人工版
- **Self-monitoring capability**：系统识别自己无法自主完成的任务，信号转给人工操作员
- **Trusted testers + opt-in businesses**：先小范围测试，再逐步扩大
- **渐进式域扩展**：从 "确认节假日营业时间" 起步，再扩展到预约

**启示**：Duplex 的做法本质是 **shadow + human-in-the-loop**，和我们 "shadow 3 天 + 人工审核灰区" 高度对齐。

### 1.4 OpenAI

OpenAI 没有公开的 shadow/canary 机制用于 function calling。他们的 [Evaluation Best Practices](https://platform.openai.com/docs/guides/evaluation-best-practices) 推荐分层评估：

1. **Metric-based evals**（ROUGE, exact match, function call accuracy）
2. **LLM-as-a-judge**（pairwise comparison / single scoring / reference-guided）
3. **Human evals**（最高质量但最贵最慢）

> Strong LLM judges like GPT-4.1 can match both controlled and crowdsourced human preferences, achieving over 80% agreement (the same level of agreement between humans). — OpenAI Docs

### 1.5 AWS SageMaker

AWS 提供原生 [Shadow Variant](https://docs.aws.amazon.com/sagemaker/latest/dg/model-shadow-deployment.html) 功能：候选模型作为 shadow variant 附加到 endpoint，自动接收生产流量副本，输出记录到 S3 供分析。完全托管，不需自建路由。

---

## 二、Q1 — 主流对齐判断方法

| 方法 | 延迟 | 成本/次 | 准确率 | 适用场景 |
|------|------|---------|--------|---------|
| **String exact match** | <1ms | $0.00 | 100%（但 recall 极低） | 结构化输出（JSON key、枚举值） |
| **Regex / 关键实体提取** | <1ms | $0.00 | ~85% on structured | skill_name、参数名、数值 |
| **Jaccard / keyword overlap** | <1ms | $0.00 | ~70% general | 粗筛 |
| **Embedding cosine similarity** | ~10ms | ~$0.00002 | ~80-85% | 语义相似度粗筛 |
| **BERTScore** | ~50ms | $0.00（本地） | ~85% | 参考答案比对 |
| **LLM-as-judge (GPT-4o-mini)** | 1-3s | $0.003-0.01 | ~85-90% human agreement | 语义等价判断 |
| **LLM-as-judge (GPT-4o)** | 2-5s | $0.03 | ~90%+ human agreement | 高精度判断 |
| **LLM-as-judge (GPT-o3-mini)** | ~16s | $0.02 | ~90%+ | 推理密集判断 |
| **Human eval** | ~600s | ~$50.00 | Gold standard | 最终仲裁 |

（成本数据来源：[Nature 2025 Table 3](https://www.nature.com/articles/s41746-025-02005-2/tables/3), [Iris eval blog](https://iris-eval.com/blog/heuristic-vs-semantic-eval), [OpenAI Docs](https://platform.openai.com/docs/guides/evaluation-best-practices)）

**业界共识**：不存在单一最佳方法，**分层组合**是标准做法。

---

## 三、Q2 — LLM-as-Judge 准确率与成本

### 准确率

| 指标 | 数值 | 来源 |
|------|------|------|
| GPT-4.1 与人类偏好一致性 | **>80%** | OpenAI Eval Docs |
| G-Eval (CoT) 单输出评分 | **77.5%** human alignment | Arena G-Eval 研究 |
| Arena G-Eval (pairwise) winner selection | **95%** agreement | Arena G-Eval 研究 |
| RAG faithfulness scoring | **90%** human alignment | Comet blog |
| 人类之间的一致性（ceiling） | **~80%** | 多项研究 |

**关键发现**：LLM judge 已经达到人类 inter-annotator agreement 水平（~80%），不需要超越人类。

### 成本

| Judge Model | 策略 | 平均耗时 | 成本/次 |
|-------------|------|---------|---------|
| Human | — | 600s | $50.00 |
| GPT-4o | Zero-shot | 12s | $0.03 |
| GPT-4o | Few-shot | 14s | $0.10 |
| GPT-o3-mini | Zero-shot | 16s | $0.02 |
| GPT-4o-mini | Zero-shot | ~3s | **$0.003-0.005** |
| Haiku 3.5 | Zero-shot | ~2s | **$0.002-0.004** |

（来源：[Nature 2025](https://www.nature.com/articles/s41746-025-02005-2/tables/3)）

> LLM-as-a-judge uses pre-trained models to evaluate responses automatically, providing human-like evaluation quality with up to 98% cost savings. — AWS

### 最佳 Prompt 模式

OpenAI 推荐（[Eval Best Practices](https://platform.openai.com/docs/guides/evaluation-best-practices)）：

1. **Pairwise comparison > pointwise scoring**（更稳定，LLM 更擅长比较而非绝对打分）
2. **Chain-of-thought reasoning before scoring**（先推理再给分，提升准确率 +12pp）
3. **Swap order to mitigate position bias**（跑两次交换顺序取平均）
4. **最强模型做 judge**（如果预算允许，用 o3/GPT-4o 而不是 mini）

---

## 四、Q3 — 更便宜的近似方案

### 4.1 三层评估架构（推荐）

这是 Iris eval 框架和多个生产系统验证的模式（[Iris blog](https://iris-eval.com/blog/heuristic-vs-semantic-eval)）：

```
Layer 1: 结构化匹配（$0.00, <1ms）——处理 ~60% 的评估
    ├── skill_name exact match
    ├── 参数 key 集合匹配
    ├── 数值/枚举值 exact match
    └── JSON schema validation

Layer 2: Embedding 相似度（~$0.00002, ~10ms）——处理 ~25% 的评估
    ├── text-embedding-3-small cosine similarity
    ├── 阈值 > 0.85 → PASS
    ├── 阈值 < 0.60 → FAIL
    └── 中间灰区 → 送 Layer 3

Layer 3: LLM-as-judge（$0.003, ~2s）——仅处理 ~15% 的灰区
    ├── GPT-4o-mini / Haiku pairwise comparison
    ├── "这两个回答在语义上是否等价？"
    └── 附带 reasoning 输出供人工审核
```

**成本估算**（假设每天 100 次 skill 调用 × 3 天 shadow）：
- Layer 1: 300 × 60% = 180 次 × $0.00 = **$0.00**
- Layer 2: 300 × 25% = 75 次 × $0.00002 = **$0.0015**
- Layer 3: 300 × 15% = 45 次 × $0.003 = **$0.135**
- **总计：~$0.14 for 3 天 shadow testing**

### 4.2 关键实体提取对比（最便宜的语义方案）

从两个输出中提取关键实体，结构化后精确比较：

```python
def extract_entities(response: str) -> dict:
    """从 LLM 回答中提取结构化实体"""
    return {
        "action": "turn_on",           # 动作意图
        "device": "living_room_light", # 目标设备
        "params": {"brightness": 80},  # 参数
        "sentiment": "positive",       # 情感极性
        "has_explanation": True,        # 是否包含解释
    }

def compare_entities(a: dict, b: dict) -> float:
    """结构化对比，返回 0-1 对齐分数"""
    score = 0
    if a["action"] == b["action"]: score += 0.4      # 动作最重要
    if a["device"] == b["device"]: score += 0.3      # 设备次之
    if a["params"] == b["params"]: score += 0.2      # 参数
    if a["sentiment"] == b["sentiment"]: score += 0.1 # 情感
    return score
```

**优点**：$0.00/次，<1ms，完全确定性
**缺点**：需要为每种 skill 类型定义提取规则；自然语言回答部分（say 内容）难以结构化

### 4.3 BERT-as-a-Judge（中间路线）

[arXiv 2604.09497](https://arxiv.org/pdf/2604.09497) 提出用 BERT 替代 LLM judge：
- **本地运行**，无 API 成本
- 比 BLEU/ROUGE 等词汇方法准确
- 比 LLM-as-judge 便宜 100x+
- 适合高吞吐量场景

### 4.4 方法选择决策树

```
新 skill 输出是结构化的吗？（JSON / tool_call）
  ├── YES → Layer 1 结构化匹配就够了（免费）
  └── NO → 自然语言回答
       ├── 有参考答案吗？
       │    ├── YES → embedding similarity（几乎免费）
       │    └── NO → LLM-as-judge（$0.003/次）
       └── 是否安全/关键场景？
            ├── YES → LLM-as-judge + 人工审核
            └── NO → embedding similarity + 阈值
```

---

## 五、针对 Jarvis Skill Shadow 的具体建议

### 5.1 我们的场景特点

- 新编译的 skill 在 shadow 模式运行 3 天
- 和 Cloud LLM（Grok）的输出做对比
- 对齐率达标才晋升 live
- 每天调用量不大（预估 50-200 次）

### 5.2 推荐方案

```yaml
shadow_testing:
  duration_days: 3
  min_samples: 30          # 最少需要 30 次调用才能评估
  
  evaluation:
    # Layer 1: 结构化匹配（所有 tool_call 类型的 skill）
    structural:
      check_skill_name: true       # skill 名字必须一致
      check_param_keys: true       # 参数 key 集合必须一致
      check_param_values: "fuzzy"  # 数值允许 ±5% 偏差
      weight: 0.5
    
    # Layer 2: 语义对比（say 内容等自然语言输出）
    semantic:
      method: "embedding"          # text-embedding-3-small
      model: "text-embedding-3-small"
      threshold_pass: 0.85
      threshold_fail: 0.60
      weight: 0.3
    
    # Layer 3: LLM judge（仅灰区）
    llm_judge:
      model: "gpt-4o-mini"
      trigger: "gray_zone"         # 仅在 Layer 2 灰区触发
      prompt: "pairwise_equivalence"
      weight: 0.2
  
  promotion:
    overall_alignment: 0.85        # 总对齐率 ≥ 85%
    critical_skill_alignment: 0.95 # 关键 skill（设备控制）≥ 95%
    zero_safety_failures: true     # 安全类 0 失败
    
  rollback:
    auto_rollback_threshold: 0.60  # 对齐率 < 60% 自动回滚
    alert_threshold: 0.75          # < 75% 通知用户
```

### 5.3 成本预估

| 场景 | 每日调用 | 3 天总成本 |
|------|---------|-----------|
| 轻度使用（50/天） | 150 次 | **~$0.07** |
| 中度使用（200/天） | 600 次 | **~$0.28** |
| 重度使用（500/天） | 1500 次 | **~$0.70** |

几乎可以忽略不计。

---

## 六、和现有架构的集成点

```
现有流程：
  User request → Intent Router → Skill Execution → TTS → Response

Shadow 流程：
  User request → Intent Router → [Live Skill + Shadow Skill] parallel
                                      ↓              ↓
                                   返回用户       仅记录
                                                    ↓
                                              Evaluator
                                           (3-layer compare)
                                                    ↓
                                              SQLite log
                                                    ↓
                                          3 天后 → 晋升决策
```

**关键实现细节**：
1. Shadow skill 异步执行，不影响主路径延迟
2. 评估结果写入 SQLite（复用现有 memory DB）
3. 晋升决策可以自动或人工确认（建议关键 skill 人工）

---

## Sources

1. [tianpan.co — Shadow Mode, Canary, A/B Testing for LLMs](https://tianpan.co/blog/2026-04-09-llm-gradual-rollout-shadow-canary-ab-testing) — 最完整的 LLM 部署指南
2. [Uber — Raising the Bar on ML Model Deployment Safety](https://www.uber.com/blog/raising-the-bar-on-ml-model-deployment-safety/) — 75% shadow coverage, safety scoring
3. [Google Research — Google Duplex](https://research.google/blog/google-duplex-an-ai-system-for-accomplishing-real-world-tasks-over-the-phone/) — real-time supervised training
4. [OpenAI — Evaluation Best Practices](https://platform.openai.com/docs/guides/evaluation-best-practices) — 分层评估框架
5. [AWS — SageMaker Shadow Testing](https://docs.aws.amazon.com/sagemaker/latest/dg/model-shadow-deployment.html) — 托管 shadow variant
6. [Iris Eval — Heuristic vs Semantic](https://iris-eval.com/blog/heuristic-vs-semantic-eval) — 80/20 composite 架构
7. [Nature 2025 — LLM-as-Judge Costs Table](https://www.nature.com/articles/s41746-025-02005-2/tables/3) — GPT-4o $0.03, o3-mini $0.02/eval
8. [ScienceDirect — Survey on LLM-as-a-Judge](https://www.sciencedirect.com/science/article/pii/S2666675825004564) — 系统综述
9. [Comet — LLM-as-a-Judge Ultimate Guide](https://www.comet.com/site/blog/llm-as-a-judge/) — G-Eval 77.5%, Arena 95%
10. [MarkTechPost — Four Controlled Strategies](https://www.marktechpost.com/2026/03/21/safely-deploying-ml-models-to-production-four-controlled-strategies-a-b-canary-interleaved-shadow-testing/) — 标准四阶段
11. [arXiv 2604.09497 — BERT-as-a-Judge](https://arxiv.org/pdf/2604.09497) — 本地 BERT 替代 LLM judge
12. [Agenta — Text Similarity Evaluators](https://docs.agenta.ai/evaluation/evaluators/semantic-similarity) — embedding $0.02/1M tokens

## Methodology

4 条 exa 搜索 query 并行，3 个关键页面深度抓取，交叉验证成本数据和准确率数字。
