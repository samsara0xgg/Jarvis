# Research Pack 4 — Grok-4.1-fast and Claude Sonnet 4.6 Chinese Long-Context Data

**Date:** 2026-04-14
**Purpose:** Verify Chinese-language and long-context benchmark data for Jarvis LLM decisions.
**Methodology:** Official model cards/PDFs (VERIFIED), vendor blogs (VERIFIED), third-party benchmarks (VERIFIED when reproducible), community reviews (UNVERIFIED).

---

## TL;DR

- **Grok-4.1-fast Chinese data: essentially NONE from xAI.** xAI's Nov 17 2025 Grok 4.1 model card contains zero Chinese benchmark numbers, zero MMLU/MMMLU, zero needle-in-haystack. Only safety/CBRN evaluations are published. The one Chinese-language third-party test (ITSoloTime, ~15k-question internal benchmark) puts **grok-4-1-fast-reasoning at 64.3 % accuracy — rank 29 of 21-ish mainstream models**, and the non-reasoning variant at **47.6 % (-14.1 pp vs Grok-3-mini)**. Language/instruction-following dropped 24.3 pp in the non-reasoning variant.
- **Grok 4.1 Fast ≠ distilled Grok 4.1.** It is a separately-trained agentic/tool-calling sibling with the same 2 M context but a different skill mix (better tool use, worse creative/math/language vs full Grok 4.1 Thinking).
- **Claude Sonnet 4.6 Chinese data: much better documented.** Official system card (Feb 17 2026) reports **MMMLU 89.3 %** (avg across 14 non-English languages), **GMMLU high-resource avg 91.0 %** (Chinese is in this tier, gap -1.9 pp vs English 92.9 %), **harmless-response rate Chinese 99.29 %**, **over-refusal Chinese 0.34 %**.
- **Claude Sonnet 4.6 long-context: VERIFIED.** MRCR v2 8-needle at 256 K = **90.6 % (64 k thinking)** / **90.3 % (max)**; at 1 M = **65.1 % / 65.8 %**. Sonnet 4.5 was 10.8 % / 18.5 % at the same settings — **Sonnet 4.6 is a ~4-8× jump**. Context window officially 1 M tokens.
- **Known Sonnet 4.6 issue:** Under weak/empty system prompt, short Chinese identity questions (e.g. "你是什么模型？") can trigger a "我是 DeepSeek-V3" response. Documented since Jul 2025, unpatched as of release. Not a model-substitution issue — identity anchoring bug. (UNVERIFIED as a benchmark failure; behavior reproducible on open API.)
- **SuperCLUE coverage is sparse for both.** SuperCLUE 2025 annual (released Feb 2026) ranked **Claude-Opus-4.5-Reasoning = 68.25 (#1)**. No Claude Sonnet 4.6 entry, no Grok entries at all in top rankings.

For Jarvis (Chinese voice assistant, typical 1-4 k prompt context), Sonnet 4.6's published Chinese numbers are strong and trustworthy. Grok-4.1-fast is under-documented for Chinese; the one independent Chinese test (ITSoloTime) shows middling results and a sharp drop in instruction-following for the non-reasoning variant — a red flag for a voice-command pipeline.

---

## Part A — Grok-4.1-fast

### A1. Does xAI publish Chinese benchmark scores? NOT FOUND (VERIFIED absence)

Full text extraction of the **official Grok 4.1 Model Card (Nov 17 2025)** — [data.x.ai/2025-11-17-grok-4-1-model-card.pdf](https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf) — contains:

| Section | Contents |
|---|---|
| Abuse Potential | Refusals, jailbreaks, AgentDojo prompt-injection (multilingual refusal corpus inc. Chinese, but **only aggregate numbers** — no Chinese-only breakdown) |
| Concerning Propensities | MASK dishonesty (0.49 T / 0.46 NT), Sycophancy (0.19 T / 0.23 NT) |
| Dual-Use Capabilities | WMDP Bio/Chem/Cyber, VCT, BioLP, ProtocolQA, FigQA, CloningScenarios, CyBench, MakeMeSay |

> "This dataset consists of multiple languages (English, Spanish, Chinese, Japanese, Arabic, and Russian) and several thousand diverse violative prompts. In running evaluations for this latest model, we realized that results in previous model cards were reported with an error in the evaluation settings, where only the English prompts were evaluated. Here, we report true multilingual results, which are not directly comparable to previous results." — Grok 4.1 Model Card §2.1.2

**No MMLU, MMMLU, C-Eval, CMMLU, SuperCLUE, LongBench, MRCR, RULER, or needle-in-haystack numbers appear in the official card.** The card is titled "Grok 4.1" and does **not separately evaluate** Grok 4.1 Fast.

### A2. Third-party Chinese evaluations

**VERIFIED (independent, reproducible methodology):**

ITSoloTime — large-scale Chinese evaluation (~15 000 questions across reasoning/math, medical, finance, legal, language, agent domains):

| Model | Overall accuracy | Rank | Notes |
|---|---|---|---|
| **grok-4-1-fast-reasoning** | **64.3 %** | **29 of 21-ish** | 62 s/call, 2 492 tok/call, ¥8.1/1k calls |
| **grok-4-1-fast-non-reasoning** | **47.6 %** | below comparable tier | ¥1.6/1k calls, **-14.1 pp vs grok-3-mini (61.7 %)** |
| hunyuan-turbos-20250926 (ref.) | 65.9 % | — | ¥2/1k calls (4× cheaper than grok-4-1-fast-reasoning) |
| DeepSeek-V3.2, GLM-4.6, Kimi-K2 (ref.) | all > 67 % | — | — |

Per-domain breakdown for **grok-4-1-fast-reasoning** (vs grok-4-0709):

| Domain | Prev | New | Δ |
|---|---|---|---|
| Reasoning/Math | 63.7 % | 78.1 % | **+14.4** |
| Agent & Tool Calling | 48.4 % | 65.4 % | **+17.0** |
| Medical/Psychology | 75.0 % | 70.3 % | -4.7 |
| Finance | 75.1 % | 70.6 % | -4.5 |
| Legal/Admin | 74.0 % | 65.3 % | -8.7 |
| **Language / Instruction-Following** | 64.6 % | **52.8 %** | **-11.8** |

Per-domain for **grok-4-1-fast-non-reasoning** (vs grok-3-mini):

| Domain | Prev | New | Δ |
|---|---|---|---|
| Language & Instruction | 68.3 % | 44.0 % | **-24.3** |
| Reasoning/Math | 62.9 % | 45.1 % | -17.8 |
| Medical/Psychology | 65.8 % | 51.4 % | -14.4 |
| Agent | 59.7 % | 57.0 % | -2.7 |

Sources: [itsolotime.com/archives/14603](https://www.itsolotime.com/archives/14603), [itsolotime.com/archives/14607](https://www.itsolotime.com/archives/14607).

**UNVERIFIED / NOT FOUND:**
- **SuperCLUE:** No Grok entries in SuperCLUE March 2025 monthly or 2025 annual report (released Feb 2026). The [www.fxbaogao.com/detail/5255867](https://www.fxbaogao.com/detail/5255867) annual top rankings list Claude-Opus-4.5-Reasoning, Gemini-3-Pro, GPT-5.2, Kimi-K2.5-Thinking, Qwen3-Max-Thinking. Grok absent.
- **C-Eval, CMMLU:** No published score for any Grok 4.x variant found.
- **LMArena Chinese subset:** No. (Grok 4.1 Thinking = #1 overall at 1483 Elo, but this is language-agnostic.)

### A3. Context window and long-context recall

| Source | Context window | Notes |
|---|---|---|
| [x.ai/news/grok-4-1-fast](https://x.ai/news/grok-4-1-fast) (official, Nov 19 2025) | **2 M tokens** | "long-horizon RL with strong emphasis on multi-turn scenarios, ensuring consistent performance across its full 2-million-token context window" |
| [docs.oracle.com — OCI mirror](https://docs.oracle.com/en-us/iaas/Content/generative-ai/xai-grok-4-1-fast.htm) | 2 M tokens (prompt+response) | 16k output cap in playground |
| [openrouter.ai/x-ai/grok-4.1-fast](https://openrouter.ai/x-ai/grok-4.1-fast) | 2 M tokens | $0.20/M in, $0.50/M out |
| [vals.ai/models/grok_grok-4-1-fast-reasoning](https://www.vals.ai/models/grok_grok-4-1-fast-reasoning) | 2 M / 2 M output | Vals Index 49.73 % (rank 32/39); no Chinese split |
| [datastudios.org — Dec 8 2025](https://www.datastudios.org/post/xai-grok-4-1-fast-how-the-128k-context-window-and-8k-output-limit-work-for-large-chats-documents) | **128 K (claimed)** | **Contradicts all other sources — treat as erroneous/outdated blog.** |

**No public needle-in-haystack, RULER, LongBench, or LongBench-zh numbers for any Grok 4.x variant.** The long-context claim is "trained for consistency" — marketing language, not a measured score. Rahul Kolekar's playbook article explicitly notes: *"At the time of writing, I could not find a public technical report for Grok 4.1 that documents a 2-million token context window."*

### A4. Community tests (Reddit, 知乎, 微博)

- **知乎 discussions** (Nov 2025 onward) focus on Chinese access routing ("国内可用"), Grok 4.1's LMArena #1 ranking, and 256 K "long-text" capability. No quantitative Chinese-language benchmarks beyond ITSoloTime.
- **X / Medium reviews** (Barnacle Goose on Medium) note: "raw coding and math benchmarks still oddly under-documented." Grok-4.1-Fast excels at τ²-bench Telecom and Berkeley Function-Calling, **does not excel at EQ-Bench creative writing**. No multilingual/Chinese coverage.
- **LMArena (language-agnostic):** Grok 4.1 Thinking 1 483 Elo #1, Grok 4.1 Instant 1 465 Elo. Covers the full Grok 4.1 model, **not Grok 4.1 Fast specifically**.

### A5. Grok 4.1 vs Grok 4.1 Fast — relationship

**VERIFIED:** Grok 4.1 Fast is **NOT a distilled/quantized Grok 4.1.** It is a separately-trained model positioned as xAI's agentic / tool-calling model.

> "Grok 4.1 Fast is xAI's best agentic tool calling model that shines in real-world use cases like customer support and deep research." — [x.ai official](https://x.ai/news/grok-4-1-fast)

Differences:
- **Grok 4.1** (Thinking / Non-Thinking): consumer model, 256 K context, launched Nov 17 2025, best general intelligence, LMArena #1.
- **Grok 4.1 Fast** (Reasoning / Non-Reasoning): agentic model, **2 M context**, launched Nov 19 2025, best tool-calling, half the hallucination rate of Grok 4 Fast, $0.20/$0.50 per M tokens.

Per [Galaxy.ai comparative analysis](https://blog.galaxy.ai/compare/grok-4-1-fast-vs-grok-4-fast): Grok 4 Fast (predecessor) used 40 % fewer thinking tokens vs Grok 4 at comparable benchmark scores. Grok 4.1 Fast continues that agentic-specialization trajectory.

**Quality trade-off (VERIFIED via ITSoloTime):** Grok 4.1 Fast Reasoning gains +14-17 pp on math/agent vs Grok-4-0709, but **loses 8-12 pp on instruction-following, legal, finance, medical**. The non-reasoning variant regresses ~14 pp overall vs Grok-3-mini. For a Chinese voice-assistant pipeline, **the instruction-following regression is the main concern** — voice commands require reliable language understanding, not agentic math.

---

## Part B — Claude Sonnet 4.6

### B1. Chinese benchmark scores (VERIFIED from system card)

Official source: **Claude Sonnet 4.6 System Card, Feb 17 2026** — [www-cdn.anthropic.com/78073f739564e986ff3e28522761a7a0b4484f84.pdf](https://www-cdn.anthropic.com/78073f739564e986ff3e28522761a7a0b4484f84.pdf) (9.4 MB, 4 358 lines).

**§2.11 MMMLU (14 non-English languages including Chinese):**

| Model | Score |
|---|---|
| **Claude Sonnet 4.6** | **89.3 %** |
| Claude Opus 4.6 | 91.1 % |
| Claude Sonnet 4.5 | 90.8 % |
| Claude Opus 4.5 | 89.5 % |
| Gemini 3 Pro | 91.8 % |
| GPT-5.2 Pro | 89.6 % |

> "Claude Sonnet 4.6 achieved a score of 89.3 % averaged over 10 trials on all non-English language pairings, each run with adaptive thinking, max effort, and default sampling settings (temperature, top_p)." — System Card §2.11

**§2.19.1 GMMLU by resource tier** (Chinese is a high-resource language):

| Tier | Sonnet 4.6 | Gap vs EN | Sonnet 4.5 | Opus 4.6 | Gemini 3 Pro | GPT-5.2 Pro |
|---|---|---|---|---|---|---|
| English (baseline) | 92.9 % | 0.0 % | 93.1 % | 93.9 % | 94.4 % | 93.1 % |
| **High-resource avg** (inc. 中文, 日, 阿, 西, 法, 德, 葡, 俄, 意, 荷, 韩, 波, 土, 瑞, 捷) | **91.0 %** | **-1.9 %** | 91.1 % | 92.2 % | 92.9 % | 91.5 % |
| Mid-resource avg | 90.2 % | -2.7 % | 90.0 % | 91.6 % | 92.5 % | 90.9 % |
| Low-resource avg | 83.8 % | -9.1 % | 81.3 % | 85.5 % | 89.4 % | 87.2 % |

**Chinese itself is not broken out individually** — it is pooled into the "high-resource" bucket. The -1.9 pp gap vs English applies to Chinese.

**§3.1 Safety evaluations — by-language breakdown:**

| Metric | Sonnet 4.6 (Chinese) | Sonnet 4.6 (English) | Sonnet 4.5 (Chinese) |
|---|---|---|---|
| Harmless response rate (violative) | **99.29 %** | 99.39 % | 97.27 % |
| Over-refusal rate (benign) | **0.34 %** | 0.21 % | 0.13 % |

**NOT FOUND:** C-Eval, CMMLU, SuperCLUE — Anthropic does not publish these. No third-party C-Eval/CMMLU score for Sonnet 4.6 found in Apr 2026 searches.

### B2. Needle-in-haystack at 32 K, 64 K, 128 K, 200 K

Anthropic publishes **OpenAI MRCR v2 8-needle** (harder than classic NIAH — requires identifying the correct ordinal instance among similar items) at 256 K and 1 M only. No 32/64/128 K breakout is reported in the card.

**§2.16 Long context (Table 2.16.A):**

| Evaluation | Sonnet 4.6 | Opus 4.6 | Sonnet 4.5 | Gemini 3 Pro | Gemini 3 Flash | GPT-5.2 |
|---|---|---|---|---|---|---|
| MRCR v2 **256 K 8-needles** (Mean Match Ratio, 64 k thinking) | **90.6 %** | 91.9 % | **10.8 %** | 45.4 % | 58.5 % | 63.9 % (70.0 self-rep) |
| … max effort | 90.3 % | 93.0 % | — | — | — | — |
| MRCR v2 **1 M 8-needles** (64 k thinking) | **65.1 %** | 78.3 % | **18.5 %** | 24.5 % | 32.6 % | — (400 K cap) |
| … max effort | 65.8 % | 76.0 % | — | — | — | — |

> "[Figure 2.16.1.A] Claude Sonnet 4.6 is competitive with state-of-the-art Claude Opus 4.6 on long context comprehension and precise sequential reasoning measured through OpenAI MRCR v2 8 needles."

**GraphWalks (BFS) — alternative long-context test:**

| Eval | Sonnet 4.6 | Opus 4.6 | Sonnet 4.5 |
|---|---|---|---|
| GraphWalks BFS 1 M (64 k think) | 68.4 | 41.2 | 25.6 |
| GraphWalks BFS 256 K subset of 1 M (64 k think) | 72.8 | 61.5 | 44.9 |
| GraphWalks Parents 256 K subset of 1 M (64 k think) | 96.9 | 95.1 | 81.0 |

**Key takeaway for Jarvis:** Sonnet 4.6 is the first Sonnet-class model with **usable** long-context behavior — 4-8× Sonnet 4.5 on MRCR. For typical voice-assistant context (1-4 k tokens, occasionally 32 K for memory replay) we operate far below the 256 K measurement point, so practical retrieval should be excellent.

**Chinese-specific NIAH: NOT FOUND.** The MRCR test is English-only. Anthropic has not published a Chinese-language long-context benchmark result for Sonnet 4.6.

### B3. Does Anthropic's model card mention Chinese performance?

**YES.** The Sonnet 4.6 system card (Feb 17 2026) explicitly covers:

- §2.11 MMMLU (Chinese is one of 14 tested languages): 89.3 %
- §2.19 Multilingual: GMMLU high-resource tier (inc. Chinese): 91.0 %
- §3.1 Safety by language (Mandarin Chinese broken out): 99.29 % harmless, 0.34 % over-refusal
- §3.1.2 Benign-request over-refusal: 0.34 % (vs 0.13 % for Sonnet 4.5 — minor regression)

> "Claude Sonnet 4.6 showed overall meaningful improvements on this evaluation compared to Claude Sonnet 4.5. Both models performed strongly, but Sonnet 4.6 performed near-perfectly across all languages, with negligible variation among them." — §3.1.1

### B4. Third-party Chinese tests (SuperCLUE long-context, LongBench-zh)

**NOT FOUND for Sonnet 4.6:**
- **SuperCLUE 2025 annual** ([fxbaogao.com/detail/5255867](https://www.fxbaogao.com/detail/5255867), [sohu.com/a/984449661_121864792](https://www.sohu.com/a/984449661_121864792)): Top rank = **Claude-Opus-4.5-Reasoning at 68.25**, Gemini-3-Pro 65.59, GPT-5.2-high 64.32, Kimi-K2.5-Thinking 61.50, Qwen3-Max-Thinking 60.61. **Sonnet 4.6 not listed** (likely because SuperCLUE 2025 annual focused on reasoning-model leaderboards and Sonnet 4.6 released too late).
- **SuperCLUE monthly (Mar 2025)** ([fxbaogao.com/detail/4741092](https://www.fxbaogao.com/detail/4741092)): o3-mini(high) 76.01, DeepSeek-R1 70.34, Claude 3.7 Sonnet ~68. No Sonnet 4.x.
- **LongBench / LongBench-zh**: No Sonnet 4.6 score found on the standard leaderboard as of Apr 2026.
- **[awesomeagents.ai long-context leaderboard](https://awesomeagents.ai/leaderboards/long-context-benchmarks-leaderboard/)** confirms Sonnet 4.6 at MRCR v2 4-needle 256 K ≈ 82 %, 8-needle 1 M cited from system card. No Chinese subset.

### B5. Compare to Claude Sonnet 4.5

Sonnet 4.5 has substantially weaker long-context performance on the same metrics:

| Metric | Sonnet 4.5 | Sonnet 4.6 | Improvement |
|---|---|---|---|
| MMMLU (avg non-English) | 90.8 % | 89.3 % | **-1.5 pp (regression)** |
| MRCR v2 256 K 8-needle | 10.8 % | 90.6 % | **+79.8 pp (8.4×)** |
| MRCR v2 1 M 8-needle | 18.5 % | 65.1 % | **+46.6 pp (3.5×)** |
| GraphWalks BFS 1 M | 25.6 % | 68.4 % | **+42.8 pp (2.7×)** |
| Chinese harmless rate (violative) | 97.27 % | 99.29 % | +2.02 pp |
| Chinese over-refusal (benign) | 0.13 % | 0.34 % | -0.21 pp (slight regression) |
| Context window | 200 K / 1 M (beta) | **1 M (default)** | — |

**Note:** The MMMLU regression from 4.5 → 4.6 (-1.5 pp) is real but small and likely driven by Sonnet 4.6's different training data mix. Practical Chinese output quality is comparable to better on community reports.

### B6. Known Sonnet 4.6 Chinese issue — "I'm DeepSeek" identity leak

**VERIFIED reproducible behavior, NOT a benchmark failure.** Multiple Reddit/知乎 threads (Jul 2025 – Feb 2026) show Sonnet 4.6 answering `你是什么模型？` with "我是 DeepSeek-V3" under weak/empty system prompts. Coverage:

- [ucstrategies.com/news/claude-sonnet-4-6-crushes-benchmarks-but-thinks-its-deepseek…](https://ucstrategies.com/news/claude-sonnet-4-6-crushes-benchmarks-but-thinks-its-deepseek-when-you-prompt-in-chinese/) — calls it "a prompt injection flaw that's gone unpatched since July 2025"
- [blog.laozhang.ai/en/posts/claude-sonnet-4-6-says-deepseek](https://blog.laozhang.ai/en/posts/claude-sonnet-4-6-says-deepseek) — interprets as "identity confusion under weak prompt boundary, not model substitution … short Chinese self-identification prompts can be answered from learned language patterns rather than from a clean product identity trace"

**Not documented in the official system card.** Mitigation: any Jarvis deployment that surfaces identity (e.g. "你叫什么名字？" triggers a scripted response) must **hard-code identity in the system prompt** rather than relying on Sonnet 4.6 to introduce itself correctly in Chinese.

---

## Part C — Cross-comparison summary

### C1. Published Chinese scores (VERIFIED)

| Benchmark | Grok-4.1-fast | Sonnet 4.6 | Sonnet 4.5 | Opus 4.6 |
|---|---|---|---|---|
| MMMLU (14-lang avg, inc. Chinese) | **NOT PUBLISHED** | 89.3 % | 90.8 % | 91.1 % |
| GMMLU Chinese-tier (high-resource avg) | NOT PUBLISHED | 91.0 % | 91.1 % | 92.2 % |
| SuperCLUE | NOT LISTED | NOT LISTED | NOT LISTED | Opus 4.5 only: 68.25 |
| C-Eval / CMMLU | NOT PUBLISHED | NOT PUBLISHED | NOT PUBLISHED | NOT PUBLISHED |
| ITSoloTime 15k-question CN eval | 64.3 % (R) / 47.6 % (NR) | — | — | — |
| Chinese violative-prompt harmless-rate | NOT PUBLISHED | 99.29 % | 97.27 % | 99.63 % |
| Chinese benign-prompt over-refusal | NOT PUBLISHED | 0.34 % | 0.13 % | 0.52 % |

### C2. Long-context (ANY language — VERIFIED)

| Benchmark | Grok-4.1-fast | Sonnet 4.6 | Sonnet 4.5 |
|---|---|---|---|
| Context window (official) | 2 M | 1 M | 200 K (1 M beta) |
| MRCR v2 256 K 8-needle | NOT PUBLISHED | 90.6 % | 10.8 % |
| MRCR v2 1 M 8-needle | NOT PUBLISHED | 65.1 % | 18.5 % |
| GraphWalks 1 M BFS | NOT PUBLISHED | 68.4 % | 25.6 % |
| Long-horizon RL claim (marketing) | "consistent performance across full 2 M" | — | — |

### C3. Gaps and what we'd need to fabricate (we won't)

- Chinese NIAH at 32 K / 64 K / 128 K / 200 K for **either model**: NOT FOUND in public sources.
- LongBench-zh for **either model**: NOT FOUND.
- SuperCLUE long-context subset for **either model**: NOT FOUND.
- Grok-4.1-fast MMMLU / MMLU / GPQA / any academic benchmark from xAI: NOT PUBLISHED.

---

## Part D — Recommendations for Jarvis

Based on this verification pack:

1. **For main Chinese LLM** (currently Grok-4.1-fast): the Chinese-language support is under-documented and the one independent Chinese benchmark (ITSoloTime) shows middling results with a concerning instruction-following regression in the non-reasoning variant. If Jarvis uses `reasoning: false` mode, expect degraded Chinese command-following. **Action:** verify with an internal eval of 20-30 real Jarvis prompts before committing.

2. **Claude Sonnet 4.6 as Chinese fallback:** Strong, officially documented. MMMLU 89.3 %, GMMLU high-resource 91.0 %, Chinese safety 99.29 %. Long-context (if ever needed for memory replay) is **dramatically better than Sonnet 4.5** (8× on MRCR 256 K). **Caveat:** hard-code identity in system prompt to avoid the "我是 DeepSeek" bug.

3. **For 32 K-128 K Chinese recall specifically:** NO model has a published Chinese NIAH score. Sonnet 4.6 has the best published English long-context for a Sonnet-class model; reasonable inference is that Chinese performance is comparable (Chinese is a high-resource language for all major models). For production use, **run a small internal Chinese NIAH test** — this is the only way to get reliable numbers.

4. **SuperCLUE 2025 annual rankings are a useful sanity reference** even though neither candidate is listed:
   - Claude Opus 4.5 Reasoning: 68.25 (#1 global)
   - Kimi-K2.5-Thinking: 61.50 (best open-source)
   - If Chinese-native strength is prioritized, **DeepSeek-V3.2 / Kimi-K2.5 / GLM-4.6 / Qwen3-Max** are the documented leaders. Consider Groq-hosted Kimi or DeepSeek as a Jarvis fallback layer.

---

## Sources

**Official model cards (VERIFIED):**
- Grok 4.1 Model Card (Nov 17 2025) — https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf
- Claude Sonnet 4.6 System Card (Feb 17 2026) — https://www-cdn.anthropic.com/78073f739564e986ff3e28522761a7a0b4484f84.pdf

**Vendor announcements (VERIFIED):**
- Grok 4.1 Fast and Agent Tools API — https://x.ai/news/grok-4-1-fast
- Grok 4.1 — https://x.ai/news/grok-4-1
- Introducing Claude Sonnet 4.6 — https://www.anthropic.com/news/claude-sonnet-4-6
- What's new in Claude 4.6 — https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-6
- Claude models overview — https://platform.claude.com/docs/en/about-claude/models/overview

**Third-party Chinese benchmarks:**
- ITSoloTime grok-4-1-fast-reasoning review — https://www.itsolotime.com/archives/14603
- ITSoloTime grok-4-1-fast-non-reasoning review — https://www.itsolotime.com/archives/14607
- SuperCLUE 2025 annual report — https://www.fxbaogao.com/detail/5255867 · https://www.sohu.com/a/984449661_121864792
- SuperCLUE March 2025 monthly — https://www.fxbaogao.com/detail/4741092
- SuperCLUE official portal — https://www.superclueai.com/ · https://www.cluebenchmarks.com/superclue.html

**Long-context leaderboards:**
- Awesome Agents long-context leaderboard — https://awesomeagents.ai/leaderboards/long-context-benchmarks-leaderboard/
- Artificial Analysis multilingual — https://artificialanalysis.ai/models/multilingual
- Vals AI Grok 4.1 Fast Reasoning — https://www.vals.ai/models/grok_grok-4-1-fast-reasoning

**Community / identity-bug coverage:**
- Claude Sonnet 4.6 DeepSeek identity issue — https://ucstrategies.com/news/claude-sonnet-4-6-crushes-benchmarks-but-thinks-its-deepseek-when-you-prompt-in-chinese/
- Identity-bug analysis — https://blog.laozhang.ai/en/posts/claude-sonnet-4-6-says-deepseek

**Reference / context window cross-check:**
- OpenRouter Grok 4.1 Fast — https://openrouter.ai/x-ai/grok-4.1-fast
- Oracle OCI Grok 4.1 Fast — https://docs.oracle.com/en-us/iaas/Content/generative-ai/xai-grok-4-1-fast.htm
- Galaxy.ai Grok 4.1 Fast vs Grok 4 Fast — https://blog.galaxy.ai/compare/grok-4-1-fast-vs-grok-4-fast
- DataStudios 128 K claim (contradicted elsewhere — treat as erroneous) — https://www.datastudios.org/post/xai-grok-4-1-fast-how-the-128k-context-window-and-8k-output-limit-work-for-large-chats-documents
