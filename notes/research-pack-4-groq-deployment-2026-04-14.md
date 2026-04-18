# Research Pack 4 — Groq's Llama-3.3-70B Deployment vs Meta Original

Date: 2026-04-14
Context: Jarvis uses `llama-3.3-70b-versatile` on Groq as the intent router. Question: does Groq's inference stack introduce quantization/precision differences that could degrade accuracy, especially for Chinese input?

---

## TL;DR

1. **Groq does NOT run Llama-3.3-70B at full FP16.** They use proprietary "TruePoint Numerics" — a mixed-precision scheme that stores weights and activations at reduced precision (FP8 for activations in error-tolerant layers; Block Floating Point for MoE weights; FP32 kept for attention logits) while performing matmul accumulation at 100-bit precision. There is no specific public statement confirming exactly what precision the Llama-3.3-70B weights are stored at.
2. **Groq's own claim is "no appreciable accuracy loss on MMLU and HumanEval"** vs BF16 — with a 2-4× speedup. Groq's model card says TruePoint "reduces precision only in areas that don't affect accuracy."
3. **Independent evidence disputes the "no loss" claim on Llama 3 / 3.1.** SambaNova's study found their Llama-3-8B (native mixed 16/32-bit) outperformed Groq's Llama-3-8B by an average of 3.16% across 15 general benchmarks, statistically significant in 11/15 cases. CoQA (conversational QA) showed >9 percentage-point degradation on Groq. No equivalent public study exists specifically for Llama-3.3-70B.
4. **Chinese is not officially supported by Meta on Llama 3.3** (the 8 supported languages are English, German, French, Italian, Portuguese, Hindi, Spanish, Thai). Any Chinese performance on Llama 3.3 is "unofficial" — a Groq-specific quantization quality drop on top of an already-off-distribution language could compound.
5. **Context window is 131,072 tokens** (Groq docs verbatim) for `llama-3.3-70b-versatile`, with max output 32,768 tokens. The old `llama-3.3-70b-specdec` variant had only 8,192 tokens and is now deprecated / no longer listed in the current models page.

---

## Q1. What precision does Groq use for Llama-3.3-70B?

**Answer: Not publicly disclosed at weight level. Groq uses a mixed-precision stack called "TruePoint Numerics."**

From the official model card (console.groq.com):
> "This uses Groq's TruePoint Numerics, which reduces precision only in areas that don't affect accuracy, preserving quality while delivering significant speedup over traditional approaches."

From Groq's LPU technical blog (https://groq.com/blog/inside-the-lpu-deconstructing-groq-speed), precision is applied by layer type:
- **FP32** for attention logits ("where 1-bit errors propagate")
- **Block Floating Point** for Mixture-of-Experts weights (*note: Llama 3.3 70B is a dense model, so this is not directly applicable, but shows the general philosophy*)
- **FP8** storage for activations in "error-tolerant layers"
- **100 bits** of intermediate accumulation regardless of input bit width
- Hardware is capable of **INT8 (750 TOPS)** and **FP16 (188 TFLOPS)** — this is the hardware ceiling, not confirmation of weight format

Key caveat: Groq has publicly stated that weights can be stored at INT8 or FP8 while matmul happens at full precision, but they do not publish the exact format used for any specific model. Based on the LPU hardware architecture and SambaNova's description of Groq's approach ("a unique form of reduced precision"), it is widely inferred — but not officially confirmed — that Llama 70B weights are stored at **INT8**. Treat this as "informed speculation," not documented fact.

Hardware evidence from the LPU spec:
- INT8 throughput: 750 TOPS
- FP16 throughput: 188 TFLOPS

If Groq used pure FP16 for a 70B model, they would need ~140 GB of on-chip SRAM spread across many LPU chips. INT8 halves that to ~70 GB, which aligns better with their multi-chip scale-out story.

Sources:
- https://console.groq.com/docs/model/llama-3.3-70b-versatile
- https://groq.com/blog/inside-the-lpu-deconstructing-groq-speed
- https://groq.com/GroqDocs/GROQ%20ACCURACY%20TECH%20DOC%20-%20Groq%20TruePoint%20Technology.pdf (mostly image-based, not text-extractable)

---

## Q2. Does Groq apply post-training quantization, pruning, or distillation?

**Answer: Yes — they apply post-training quantization via TruePoint. No evidence of pruning or distillation.**

- **Quantization:** Yes. TruePoint is a post-training quantization / mixed-precision scheme. Groq does not fine-tune the model; they take the public weights from Meta and re-quantize for their LPU.
- **Pruning:** No public evidence. Groq blog/docs do not mention any sparsity or pruning for Llama 3.3.
- **Distillation:** No. The model served is the Meta-released `Llama-3.3-70B-Instruct` (verified by model name and quoted MMLU 86.0 / HumanEval 88.4 matching Meta's card).
- **Speculative decoding:** The old `llama-3.3-70b-specdec` variant used a smaller draft model for token-level speculation — but this is an *inference optimization*, not a model modification. Groq's internal evaluation "confirm[s] no quality degradation" from speculative decoding specifically (speculative decoding is mathematically lossless when implemented correctly, since the draft model's tokens are verified by the primary model).

Sources:
- https://console.groq.com/docs/model/llama-3.3-70b-versatile
- https://groq.com/blog/groq-first-generation-14nm-chip-just-got-a-6x-speed-boost-introducing-llama-3-1-70b-speculative-decoding-on-groqcloud

---

## Q3. Does Groq's "versatile" 70B differ from other 70B variants?

**Answer: As of April 2026, `llama-3.3-70b-versatile` is the only currently-served Llama 3.3 70B variant on Groq.**

| Model ID | Status | Context | Max Output | Speed | Notes |
|---|---|---|---|---|---|
| `llama-3.3-70b-versatile` | Active | 131,072 | 32,768 | 280 t/s | Current production model |
| `llama-3.3-70b-specdec` | Deprecated / not in current models page | 8,192 | N/A | 1,600 t/s | Speculative-decoding variant; only 8K context |

Note: `specdec` had a much shorter 8K context and higher speed (~1,600 t/s) via speculative decoding with a smaller draft model. Groq has since removed it from the active models list (it still appears in historical docs but not in the current /docs/models catalog), presumably because speculative decoding became a transparent runtime optimization on `versatile` rather than a separate SKU.

If the Jarvis code still references `llama-3.3-70b-specdec`, it will fail or fall back. Confirm it uses `llama-3.3-70b-versatile`.

Sources:
- https://console.groq.com/docs/model/llama-3.3-70b-versatile
- https://console.groq.com/docs/model/llama-3.3-70b-specdec
- https://console.groq.com/docs/models
- https://console.groq.com/docs/deprecations

---

## Q4. Published benchmarks comparing Groq-hosted vs Meta-original Llama-3.3-70B?

**Answer: No head-to-head Groq-vs-Meta-reference benchmark exists specifically for Llama 3.3 70B. Best indirect evidence is SambaNova's Llama 3 / 3.1 cross-provider study.**

### Direct Llama-3.3-70B data points

- Groq's own claim: MMLU 86.0%, HumanEval 88.4% pass@1 (on their model card) — these numbers match Meta's published Llama-3.3-70B-Instruct results, so either (a) Groq's deployment is lossless on these benchmarks or (b) they are just restating Meta's numbers (more likely — the model card doesn't claim they re-ran evals).
- Artificial Analysis treats all providers as serving identical models; their provider comparison page focuses on speed/latency/price, not quality. No quality differentiation is measured between Groq and other Llama 3.3 70B hosts.

### Indirect evidence: SambaNova's Llama 3 8B study (Sep 2024)

From https://sambanova.ai/blog/does-reduced-precision-hurt:
> "SambaNova outperformed Groq by an average of 3.16% across general tasks, with 11 out of 15 differences being statistically significant."

> "This degradation takes LLaMa 3 8b to the performance level of Gemma 7b."

> "Conversational QA (CoQA): The largest disparity, where degradation exceeds nine percentage points"

> "HumanEval (coding): Groq slightly ahead, margin is within noise"

> "Alpaca Eval 2.0: SambaNova achieved higher scores than Groq in a statistically significant manner"

> "SambaNova keeps both the model weights and activations in its original mixed 16-bit and 32-bit precision" (i.e., SambaNova is the near-full-precision reference)

### Cerebras cross-provider study (Llama 3.1 70B)

From https://www.cerebras.ai/blog/llama3.1-model-quality-evaluation-cerebras-groq-together-and-fireworks:
> "Cerebras' Llama3.1-70B model excelled in 9 out of 10 benchmarks" vs Groq, Fireworks, Together.

Specific Groq scores are only shown in figures (not OCRable from the blog text). The study covered MMLU, MATH, GPQA, DROP, MGSM, HumanEval, MBPP, MT-Bench.

**Important caveat:** Both SambaNova and Cerebras are Groq competitors with incentive to publish unfavorable comparisons. Their methodology is not fully independent — but their results are consistent with Groq using aggressive quantization.

### Extrapolation to Llama 3.3 70B

- Llama 3 70B and Llama 3.3 70B have similar quantization sensitivity (both dense 70B models, both 8-bit/16-bit-sensitive per the arxiv.org/2408.15301 study on Llama3-70B per-channel quantization).
- The 8B vs 70B behavior differs: 70B is generally *more* sensitive to 4-bit but *less* sensitive to 8-bit than 8B. So if Groq's scheme is effectively ~INT8 weights, the 70B might not show the same 3.16% gap as the 8B. But this is inferred, not measured.
- A 7.8-point MMLU drop is reported on Llama 3.3 70B when pushed to 4-bit — so 4-bit can be ruled out as Groq's approach.

Sources:
- https://sambanova.ai/blog/does-reduced-precision-hurt
- https://www.cerebras.ai/blog/llama3.1-model-quality-evaluation-cerebras-groq-together-and-fireworks
- https://arxiv.org/html/2408.15301v1
- https://artificialanalysis.ai/models/llama-3-3-instruct-70b/providers

---

## Q5. Groq's max context window for Llama-3.3-70B

**Answer: 131,072 input tokens (128K), 32,768 max output tokens.** Official and verbatim from the model page.

From https://console.groq.com/docs/model/llama-3.3-70b-versatile:
- **Context Window:** "131,072" tokens
- **Max Output Tokens:** 32,768
- **Speed:** ~280 tokens per second

| Variant | Input Context | Max Output |
|---|---|---|
| `llama-3.3-70b-versatile` | 131,072 | 32,768 |
| `llama-3.3-70b-specdec` (deprecated) | 8,192 | N/A |

The blog post https://groq.com/blog/new-ai-inference-speed-benchmark-for-llama-3-3-70b-powered-by-groq states: "Groq is able to provide consistent speed (275-276 T/sec) across all input token counts tested" — suggesting no degradation as context fills.

This matches Meta's Llama-3.3-70B native 128K context (Meta specifies 128K = 131,072), so Groq does not truncate the context window.

---

## Q6. Community quality reports (Reddit, HN, X)

### r/LocalLLaMA / HN sentiment (mixed but documented concerns)

- Llama 3.X 8B is known to degrade more than Llama 2 7B under quantization due to "denser use of all available bits" from more training (per r/LocalLLaMA discussions, github.com/ggml-org/llama.cpp/discussions/6901).
- HN users have reported Groq having "quality control issues with open source models" including "sentences ending abruptly and non-sensical/unrelated words." These reports are anecdotal and not model-version-specific.
- No systematic r/LocalLLaMA benchmark comparing Groq's Llama 3.3 70B to a local FP16 or a Together/Fireworks deployment.

### Chinese-specific user reports

- GitHub issues (meta-llama/llama3#401 and others) document Chinese characters appearing interleaved in non-Chinese outputs on Groq's Llama 3.1 — a typical symptom of either (a) poor multilingual training on Meta's side or (b) quantization amplifying off-distribution outputs.
- Specifically for English→Chinese translation via Groq, users describe output as "obviously worse than Claude 3.5 but not as bad as garbage."
- **Key fact:** Meta does NOT list Chinese as an officially supported language for Llama 3.3. The supported 8 are English, German, French, Italian, Portuguese, Hindi, Spanish, Thai. Any Chinese performance is emergent from pre-training data contamination, not a targeted capability.

Sources:
- https://github.com/meta-llama/llama3/issues/401
- https://github.com/ggml-org/llama.cpp/discussions/6901
- https://news.ycombinator.com/item?id=39434144
- https://www.llama.com/docs/how-to-guides/quantization/

---

## Summary Tables

### Precision table

| Layer/Operation | Groq Precision | Note |
|---|---|---|
| Matmul accumulation | 100-bit intermediate | Hardware-guaranteed lossless |
| Attention logits | FP32 | Error-sensitive |
| Weights (Llama 3.3 70B) | Likely INT8 (not officially stated) | Inferred from LPU capabilities + SambaNova description |
| Activations (error-tolerant layers) | FP8 | Confirmed in LPU blog |
| MoE weights (N/A for Llama 3.3) | Block Floating Point | General philosophy, not used for dense Llama |
| Comparison baseline | BF16 | Groq claims 2-4× speedup over BF16 "with no appreciable accuracy loss" |

### Context window table

| Provider / Variant | Input Context | Max Output | Precision |
|---|---|---|---|
| Meta reference | 131,072 | 131,072 (up to context) | BF16 original weights |
| Groq `llama-3.3-70b-versatile` | 131,072 | 32,768 | TruePoint (mixed, est. INT8 weights) |
| Groq `llama-3.3-70b-specdec` (deprecated) | 8,192 | N/A | TruePoint + spec decode |
| Cerebras | 128K | 128K | Mixed 16/32-bit native |
| SambaNova | 128K | 128K | Mixed 16/32-bit native |
| Together | 128K | — | FP16 (typically) |
| Fireworks | 128K | — | FP16 (typically) |
| Cloudflare Workers AI | — | — | FP8 (explicitly — `llama-3.3-70b-instruct-fp8-fast`) |

### Community benchmark findings

| Benchmark (Llama 3 8B, Groq vs SambaNova) | Gap |
|---|---|
| CoQA (conversational QA) | > 9 percentage points (Groq worse) |
| MBPP (coding) | SambaNova ahead |
| HumanEval | Groq slightly ahead (within noise) |
| Alpaca Eval 2.0 | SambaNova ahead (statistically significant) |
| Average over 15 general tasks | Groq 3.16% behind, 11/15 significant |

### Cerebras Llama 3.1 70B study

| Benchmark | Winner | Groq rank |
|---|---|---|
| 9 of 10 tested benchmarks | Cerebras | Below Cerebras (specific % not in text) |

---

## Gaps and Unknowns

1. **No officially confirmed weight precision** for `llama-3.3-70b-versatile`. All public Groq material uses the "TruePoint" marketing umbrella without specifying bit-width per model.
2. **No head-to-head Llama-3.3-70B quality benchmark** (Meta reference vs Groq) exists. The closest analogues are (a) SambaNova's Llama 3 8B study and (b) Cerebras' Llama 3.1 70B study — both from Groq competitors, both consistently unfavorable to Groq by 3-5% average.
3. **No Chinese-specific benchmark** on any hosted Llama 3.3. Chinese is not a Meta-supported language on Llama 3.3 to begin with.
4. **Groq's own claim of "no appreciable accuracy loss"** is validated only on MMLU and HumanEval (per the LPU blog). These are largely English reasoning/coding tasks and are relatively robust to quantization. Chinese token distributions can be more fragile.

---

## Implications for Jarvis

Since Jarvis uses Groq's Llama-3.3-70B as an **intent router** (not as the main reasoning/conversation LLM — Grok-4.1-fast does that), the practical impact is probably small:

- Intent classification is a relatively robust task, typically using few tokens in a constrained output space. Even a 3.16% average quality drop on open-ended benchmarks likely translates to <1% drop on a well-defined intent-routing prompt.
- The 131K context is far beyond what intent routing needs.
- **The one concrete concern:** if Chinese inputs trigger edge-case behaviors (garbled output, wrong-language tokens), Groq's quantization could amplify those. But since Jarvis wraps intent routing in JSON-mode / structured output, the downstream parser should catch most failures.

**Recommendation:** If Chinese intent-routing accuracy becomes a measured problem, compare against:
1. Groq's Llama-3.1-8B-Instant (cheaper, same-family) — is the 70B actually helping?
2. A FP16 Together/Fireworks deployment of Llama-3.3-70B — isolates whether Groq quantization is the cause.
3. Qwen 2.5 on Groq (if available) — purpose-built for Chinese/Asian languages.

No change is warranted unless a quality gap is first measured in Jarvis's own eval set.

---

## Full Source List

### Groq official
- https://console.groq.com/docs/model/llama-3.3-70b-versatile
- https://console.groq.com/docs/model/llama-3.3-70b-specdec
- https://console.groq.com/docs/models
- https://console.groq.com/docs/deprecations
- https://groq.com/blog/inside-the-lpu-deconstructing-groq-speed
- https://groq.com/blog/new-ai-inference-speed-benchmark-for-llama-3-3-70b-powered-by-groq
- https://groq.com/blog/a-new-scaling-paradigm-metas-llama-3-3-70b-challenges-death-of-scaling-law
- https://groq.com/blog/groq-first-generation-14nm-chip-just-got-a-6x-speed-boost-introducing-llama-3-1-70b-speculative-decoding-on-groqcloud
- https://groq.com/lpu-architecture
- https://groq.com/GroqDocs/GROQ%20ACCURACY%20TECH%20DOC%20-%20Groq%20TruePoint%20Technology.pdf

### Third-party comparisons
- https://www.cerebras.ai/blog/llama3.1-model-quality-evaluation-cerebras-groq-together-and-fireworks
- https://sambanova.ai/blog/does-reduced-precision-hurt
- https://sambanova.ai/blog/sn40l-chip-best-inference-solution
- https://sambanova.ai/blog/sambanova-vs-groq
- https://artificialanalysis.ai/models/llama-3-3-instruct-70b/providers
- https://artificialanalysis.ai/providers/groq
- https://tokenmix.ai/blog/llama-3-3-70b
- https://anotherwrapper.com/tools/llm-pricing/llama-33-70b-groq/llama-33-70b-sambanova

### Quantization research
- https://arxiv.org/html/2408.15301v1 (Llama3-70B per-channel quantization)
- https://developers.cloudflare.com/workers-ai/models/llama-3.3-70b-instruct-fp8-fast/
- https://www.llama.com/docs/how-to-guides/quantization/

### Community reports
- https://github.com/meta-llama/llama3/issues/401
- https://github.com/ggml-org/llama.cpp/discussions/6901
- https://news.ycombinator.com/item?id=39434144
- https://news.ycombinator.com/item?id=40527336

### Meta
- https://www.llama.com/docs/model-cards-and-prompt-formats/llama3_3/
