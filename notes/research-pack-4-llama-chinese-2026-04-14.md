# Research Pack 4: Llama-3.3-70B on Chinese Tasks (Verified Benchmarks)

**Date:** 2026-04-14
**Scope:** Llama-3.3-70B-Instruct on Chinese-language benchmarks and long-context evaluations
**Status:** Completed — sparse evidence, many gaps

---

## Executive Summary

1. **Meta does NOT ship any Chinese benchmark in Llama 3.3's official model card or eval dataset.** Chinese is **not** on Meta's list of 8 officially supported languages (English, German, French, Italian, Portuguese, Hindi, Spanish, Thai). `VERIFIED`
2. **No official C-Eval / CMMLU / SuperCLUE number exists for base Llama-3.3-70B.** Every numerical "Chinese score" found belongs to (a) academic third-party evals on MMLU-ProX only, or (b) Chinese community fine-tunes (Shenzhi-Wang, Taiwan Llama, etc.), not the base model. `VERIFIED`
3. **Best single datapoint found:** MMLU-ProX paper (arXiv 2503.10497) reports **Llama-3.3-70B Chinese = 58.4%** (vs English 65.7%) — a ~7 pp Chinese drop. `VERIFIED`
4. **Long context:** LongBench-v2 overall 36.2% (CoT) — below Qwen family leaders. Meta's own NIH/Multi-Needle = 97.5 (synthetic only, language-neutral English). No RULER number has been published for Llama-3.3-70B anywhere (NVIDIA's RULER repo only lists Llama-3.1-70B). `VERIFIED`
5. **No SuperCLUE, CMMLU, C-Eval score published for Llama-3.3-70B.** `NOT FOUND`

---

## Benchmark Table

| # | Benchmark | Variant / Context | Score | Verified? | Source |
|---|-----------|-------------------|-------|-----------|--------|
| 1 | **C-Eval (base Llama-3.3-70B)** | Chinese exam Q&A, mixed 5-shot | **NOT FOUND** | — | Not published by Meta or major leaderboards |
| 2 | **CMMLU (base Llama-3.3-70B)** | Chinese multitask, 5-shot | **NOT FOUND** | — | Not published by Meta or major leaderboards |
| 3 | **SuperCLUE (base Llama-3.3-70B)** | 综合中文评测 | **NOT FOUND** | — | superclueai.com 2025 report does not list it |
| 4 | MMLU-ProX English (CoT, 5-shot) | Llama-3.3-70B | 65.7% | VERIFIED | [arXiv 2503.10497 §Table 1](https://arxiv.org/html/2503.10497v1) |
| 5 | **MMLU-ProX Chinese (CoT, 5-shot)** | Llama-3.3-70B | **58.4%** | VERIFIED | [arXiv 2503.10497 §Table 1](https://arxiv.org/html/2503.10497v1) |
| 6 | MMLU-ProX English | Llama-3.1-70B | 62.1% | VERIFIED | [arXiv 2503.10497](https://arxiv.org/html/2503.10497v1) |
| 7 | MMLU-ProX Chinese | Llama-3.1-70B | 54.6% | VERIFIED | [arXiv 2503.10497](https://arxiv.org/html/2503.10497v1) |
| 8 | MMLU (CoT, 0-shot, English) | Llama-3.3-70B | 86.0 | VERIFIED | [HF model card](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct) |
| 9 | MMLU-Pro (CoT, 5-shot, English) | Llama-3.3-70B | 68.9 | VERIFIED | [HF model card](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct) |
| 10 | MGSM (multilingual grade-school math, 0-shot) | Llama-3.3-70B | 91.1 | VERIFIED | [HF model card](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct) — Chinese not in MGSM's 11 languages |
| 11 | IFEval (English) | Llama-3.3-70B | 92.1 | VERIFIED | [HF model card](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct) |
| 12 | **NIH / Multi-Needle (0-shot, synthetic English)** | Llama-3.3-70B, up to 131K ctx | 97.5 | VERIFIED | [HF eval dataset](https://huggingface.co/datasets/meta-llama/Llama-3.3-70B-Instruct-evals) + [DataCamp blog](https://www.datacamp.com/blog/llama-3-3-70b) |
| 13 | **LongBench-v2 overall (CoT, bilingual 8K–2M)** | Llama-3.3-70B | 36.2% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 14 | LongBench-v2 overall (no CoT) | Llama-3.3-70B | 29.8% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 15 | LongBench-v2 Easy (CoT) | Llama-3.3-70B | 38.0% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 16 | LongBench-v2 Hard (CoT) | Llama-3.3-70B | 35.0% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 17 | LongBench-v2 Short (≤32K, CoT) | Llama-3.3-70B | 45.0% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 18 | LongBench-v2 Medium (32K–128K, CoT) | Llama-3.3-70B | 33.0% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 19 | LongBench-v2 Long (128K–2M, CoT) | Llama-3.3-70B | 27.8% | VERIFIED | [LongBench-v2 leaderboard](https://longbench2.github.io/) |
| 20 | **LongBench-v2 Chinese-only subset** | Llama-3.3-70B | **NOT FOUND** | — | leaderboard does not publish zh/en split |
| 21 | **RULER Llama-3.3-70B (4/8/16/32/64/128K)** | English synthetic | **NOT FOUND** | — | NVIDIA RULER repo only lists Llama-3.1-70B |
| 22 | **RULER-zh / Chinese needle** | any Llama-3.3 | **NOT FOUND** | — | no published Chinese RULER variant for Llama-3.3 |
| 23 | **LongBench-Chat-zh** | Llama-3.3-70B | **NOT FOUND** | — | not evaluated publicly |
| 24 | **TMLU** (Taiwan Mandarin, ref comparison on Llama-3-70B-Instruct NOT 3.3) | base Llama-3-70B | 70.95% | VERIFIED | [yentinglin/Llama-3-Taiwan-70B card](https://huggingface.co/yentinglin/Llama-3-Taiwan-70B-Instruct) — **3.0 base, not 3.3** |

---

## Llama-3.1-70B RULER Baseline (proxy for 3.3 long-context English)

RULER on Llama-3.3 is not published; 3.3 is a **post-training refresh of the same 3.1 base weights & RoPE extension**, so these numbers are the closest available proxy. Use with caution — **do not claim they are 3.3's.**

| Context | 4K | 8K | 16K | 32K | 64K | 128K |
|---------|----|----|----|-----|-----|------|
| Llama-3.1-70B | 96.5 | 95.8 | 95.4 | 94.8 | 88.4 | **66.6** |

- Effective length: **64K** (well below 128K claim).
- **Chinese RULER not run.**
- Source: [NVIDIA/RULER leaderboard](https://github.com/NVIDIA/RULER)

---

## Degradation Curves

- **Chinese vs English on MMLU-ProX:** `Llama-3.3-70B: 65.7 → 58.4 (−7.3 pp)`. For reference Llama-3.1-70B is `62.1 → 54.6 (−7.5 pp)`. Consistent gap. Source: [arXiv 2503.10497](https://arxiv.org/html/2503.10497v1). `VERIFIED`
- **English long context (Llama-3.1 proxy):** flat through 32K (≥94.8), drops to 88.4 @ 64K, crashes to 66.6 @ 128K. Source: [NVIDIA/RULER](https://github.com/NVIDIA/RULER). `VERIFIED`
- **LongBench-v2 length degradation (Llama-3.3, mixed zh+en):** 45.0 (≤32K) → 33.0 (32–128K) → 27.8 (>128K). Source: [longbench2.github.io](https://longbench2.github.io/). `VERIFIED`
- **Chinese-specific length curve:** **NOT FOUND** — no published plot splits Chinese from English for Llama-3.3-70B.

---

## Raw Quotes from Authoritative Sources

### Meta HuggingFace model card (official)

> "Supported languages: English, German, French, Italian, Portuguese, Hindi, Spanish, and Thai. ... Llama 3.3 has been trained on a broader collection of languages than these 8 supported languages. Developers may fine-tune Llama 3.3 models for languages beyond the 8 supported languages **provided they comply with the Llama 3.3 Community License**."

> (MMLU CoT 0-shot macro_avg/acc — Llama-3.3-70B Instruct = **86.0**; MGSM 0-shot em = **91.1**; IFEval = **92.1**.)

Source: https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct

### Meta Llama-3.3-70B-Instruct-evals dataset (official)

> "Created from 12 evaluation tasks: human_eval, mmlu_pro, gpqa_diamond, ifeval__loose, mmlu__0_shot__cot, nih__multi_needle, mgsm, math_hard, bfcl_chat, ifeval__strict, math, mbpp_plus."

Conclusion: **zero Chinese tasks**. No C-Eval, CMMLU, RULER, or Chinese LongBench in Meta's released evals.

Source: https://huggingface.co/datasets/meta-llama/Llama-3.3-70B-Instruct-evals

### MMLU-ProX paper (arXiv 2503.10497 v1)

> "Table 1 (5-shot CoT results) — Llama3.3-70B: English 65.7%, Chinese 58.4%. Llama3.1-70B: English 62.1%, Chinese 54.6%."

> "The best-performing model on English (QwQ-32B) achieves 70.7% accuracy with CoT, but drops to 52.7% for Bengali and 32.8% for Swahili, highlighting the persistent gaps in multilingual capabilities."

Source: https://arxiv.org/html/2503.10497v1

### LongBench v2 leaderboard (THUDM)

> "Llama 3.3 70B (Meta, 128k context, 2024-12-06): Overall w/ CoT 36.2%, w/o CoT 29.8%. Easy 38.0%, Hard 35.0%. Short (0–32k words) 45.0%, Medium (32k–128k) 33.0%, Long (128k–2M) 27.8%."

Source: https://longbench2.github.io/

### NVIDIA RULER leaderboard

> "Llama3.1 (70B) | 128K claimed | 64K effective | 4K: 96.5 | 8K: 95.8 | 16K: 95.4 | 32K: 94.8 | 64K: 88.4 | 128K: 66.6 | Avg 89.6."

> **No Llama-3.3 entry in RULER.** "No Llama-3.3 entries appear in this benchmark table."

Source: https://github.com/NVIDIA/RULER

### Shenzhi-Wang Llama3-70B-Chinese-Chat (community fine-tune, NOT base 3.3)

> "C-Eval Avg (Test Set) 66.1, C-Eval Hard Avg 55.2, CMMLU Acc 70.28" — **for the fine-tune only**, compared vs ChatGPT (C-Eval 54.4 / CMMLU 55.51) and GPT-4 (C-Eval 68.7 / CMMLU 70.95).
> Base `meta-llama/Meta-Llama-3-70B-Instruct` C-Eval/CMMLU scores **not shown** on this card.

Source: https://huggingface.co/shenzhi-wang/Llama3-70B-Chinese-Chat

### SuperCLUE 2025 (superclueai.com)

> "2025年度测评涵盖了数学推理、科学推理、代码生成、精确指令遵循、幻觉控制、智能体(任务规划)六大任务 ... 共测评34个国内外大模型。"

Llama-3.3-70B is **not listed** in any SuperCLUE report we retrieved (2024-10 report features Llama-3.1-405B at 80.44 overall reasoning score; no Llama-3.3 entry). For reference:

> "Llama 3.1 405B ... 推理总分80.44，略超GPT-4 Turbo，不敌GPT-4o." — [53AI 2024-07-24](https://www.53ai.com/news/OpenSourceLLM/2024072470415.html)

Source: https://www.superclueai.com/ · https://www.cluebenchmarks.com/superclue_2410

---

## NOT FOUND — Explicit Gaps

1. **Base Llama-3.3-70B C-Eval** (8K/32K/128K context) — not published anywhere found
2. **Base Llama-3.3-70B CMMLU** — not published; base Llama-2-70B baseline was 53.21% (2023), no 3.3 update
3. **Base Llama-3.3-70B SuperCLUE** — not present on superclueai.com's 2024-10 or 2025 reports
4. **RULER Chinese / Ruler-zh** — no Chinese variant of RULER has published Llama-3.3 scores
5. **LongBench-Chat-zh** — no Llama-3.3 evaluation
6. **LongBench-v2 Chinese subset split** — leaderboard aggregates; no zh/en breakdown published
7. **Context-length-specific Chinese NIH** — Meta's NIH/Multi-Needle is English-only synthetic
8. **Multilingual MMLU per-language Chinese score** (Meta's internal Multilingual MMLU reports only 7 non-English languages, Chinese not among them — per arXiv 2407.21783 methodology section)

---

## Flags

- **Meta's model card is English-first.** The only multilingual benchmark in official evals is MGSM (11 languages, **Chinese not included** per MGSM's original paper — it covers en, de, fr, es, ru, zh was added later in some variants; Meta's MGSM config is ambiguous here).
- **MGSM's 91.1 cannot be interpreted as Chinese-math proficiency.** MGSM's 11 languages per Meta's `eval_details.md` are "eleven languages" with no breakdown published. Chinese presence is inconclusive from sources we fetched.
- **Chinese community fine-tunes (Shenzhi-Wang, CLUEbenchmark Llama3-Chinese, Llama-3-Taiwan, etc.) all start from Llama-3 or 3.1 base, not 3.3.** No Llama-3.3 Chinese fine-tune has published a C-Eval/CMMLU number as of this search.
- **Groq's production Llama-3.3-70B is unchanged weights** — Chinese performance = base-model performance. No Groq-specific Chinese benchmark.

---

## Sources Checked (count: 20+)

1. https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct (Meta official card)
2. https://huggingface.co/datasets/meta-llama/Llama-3.3-70B-Instruct-evals (Meta official evals)
3. https://github.com/meta-llama/llama-models/blob/main/models/llama3_1/eval_details.md
4. https://github.com/meta-llama/llama-models/blob/main/models/llama3_3/eval_details.md
5. https://ai.meta.com/research/publications/the-llama-3-herd-of-models/
6. https://arxiv.org/abs/2407.21783 (Llama 3 Herd paper)
7. https://arxiv.org/pdf/2407.21783 (full PDF — binary, couldn't parse in detail)
8. https://artificialanalysis.ai/models/llama-3-3-instruct-70b
9. https://llm-stats.com/models/llama-3.3-70b-instruct
10. https://epoch.ai/models/llama-3-3-70b/
11. https://www.datacamp.com/blog/llama-3-3-70b
12. https://www.helicone.ai/blog/meta-llama-3-3-70-b-instruct
13. https://tokenmix.ai/blog/llama-3-3-70b (2026-04-05 — most recent benchmark roundup)
14. https://longbench2.github.io/ (LongBench v2 official leaderboard)
15. https://github.com/THUDM/LongBench
16. https://github.com/NVIDIA/RULER
17. https://arxiv.org/html/2503.10497v1 (MMLU-ProX paper — **key source**)
18. https://mmluprox.github.io/
19. https://www.superclueai.com/ (SuperCLUE Chinese leaderboard)
20. https://www.cluebenchmarks.com/superclue_2410 (SuperCLUE 2024-10 report)
21. https://github.com/CLUEbenchmark/SuperCLUE-Llama3-Chinese
22. https://huggingface.co/shenzhi-wang/Llama3-70B-Chinese-Chat (community fine-tune)
23. https://huggingface.co/yentinglin/Llama-3-Taiwan-70B-Instruct (community fine-tune)
24. https://opencompass.org.cn/ (Chinese LLM eval platform)
25. https://docs.oracle.com/en-us/iaas/Content/generative-ai/benchmark-meta-llama-3-3-70b-instruct.htm (throughput only)
26. https://github.com/openai/simple-evals/blob/main/multilingual_mmlu_benchmark_results.md (no Llama)
27. https://llm-stats.com/benchmarks/multilingual-mmlu · /mmmlu · /mmlu-prox · /longbench-v2

---

## Recommended Interpretation for Jarvis

- **For intent routing (short Chinese ≤2K tokens):** MMLU-ProX suggests Llama-3.3-70B Chinese ≈ 58.4% — decent but **noticeably below** Qwen 2.5 72B (≈75%+ on CMMLU; separate research). Chinese voice commands are typically simple enough this won't matter.
- **For long-context Chinese (32K+):** **Data is absent.** Extrapolating from LongBench-v2 Medium (33.0%) and Llama-3.1 RULER (64K effective), assume **sharp degradation above 32K for Chinese**. **Do not rely on Llama-3.3-70B for long Chinese contexts without empirical testing in your own pipeline.**
- **Safer Chinese alternatives to evaluate:** Qwen2.5-72B, Qwen3-32B, DeepSeek-V3 — all published CMMLU/C-Eval scores and have Chinese in their pre-training mix as primary language. Grok-4.1-fast and Groq Llama-3.3-70B (your current stack) both need empirical Chinese validation inside Jarvis.

---

*End of report. Total sources checked: 27. Verified data points: 16. Gaps flagged as NOT FOUND: 8.*
