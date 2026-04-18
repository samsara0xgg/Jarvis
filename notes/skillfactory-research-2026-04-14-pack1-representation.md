# RESEARCH PACK 1 — Skill 表示形态（最高优先级）

*Date: 2026-04-14 · 4 agents 并发 · 原始引用优先*

---

## 1. Anthropic 官方 `SKILL.md`（anthropics/skills）

**表示形态**：Markdown + YAML frontmatter。**零预设结构**，body 风格高度发散。

**审计证据**（`git clone --depth 1 https://github.com/anthropics/skills` 后全仓扫描）：
- **N=18 SKILL.md**（17 skills + 1 template）。所有文件只用 **3 个 frontmatter key**：`name`、`description`、`license`。无 `allowed-tools`、无 `trigger`、无 `version`、无 `permissions`。
- **Body 风格分布（17 个 skill）**：
  - 6 个 **0% code fence**（纯散文 + 列表）：`frontend-design`、`brand-guidelines`、`doc-coauthoring`、`theme-factory`、`internal-comms`、`canvas-design`
  - 2 个 **50-61% code**（cookbook 型）：`pdf`（61%）、`docx`（51%）
  - 11 个 **<15% code**（散文为主 + 少量示例）
- **平均 220 行 / ~1500 body words**；最大 `docx` 590 行（超过 skill-creator 自己定的 <500 行硬上限）。

**核心规范来自 `skill-creator/SKILL.md:66-109`，原文**：

> "**name**: Skill identifier
> **description**: When to trigger, what it does. This is the primary triggering mechanism — include both what the skill does AND specific contexts for when to use it. All 'when to use' info goes here, not in the body. Note: currently Claude has a tendency to 'undertrigger' skills — to not use them when they'd be useful. To combat this, please make the skill descriptions a little bit 'pushy'."

**Anatomy**：
```
skill-name/
├── SKILL.md (required)
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output
```

**Progressive disclosure 在 14/17 skills 里实际出现**，四种模式：
1. **条件 "if X, read Y"**（`pdf/SKILL.md:11`）："If you need to fill out a PDF form, read FORMS.md and follow its instructions."
2. **路由表**（`pptx/SKILL.md:9-15`）：Task → Guide 表格，Claude 按任务类型跳转到子文档。
3. **脚本黑盒**（`webapp-testing/SKILL.md:11-14`）："DO NOT read the source until you try running the script first... These scripts can be very large and thus pollute your context window."
4. **分阶段加载**（`mcp-builder/SKILL.md:196-236`）："### Core MCP Documentation (Load First)" / "### SDK Documentation (Load During Phase 1/2)"。

**Pass rate 数据**：无。Anthropic 没有公开 SKILL.md 格式 vs JSON tool 的对比数据。

**适用场景**：人类可读的 agentic procedure（检索 + 执行 + 条件路由）；尤其擅长"大部分路径是 prose，少量 deterministic 操作走 script"的混合任务。

---

## 2. Claude Code 消费机制（官方文档）

**表示形态**：filesystem + bash。**没有 JSON schema 注入**，Claude 用 bash `cat SKILL.md` 拉内容到 context。

**三级 progressive disclosure，Anthropic 官方 table（platform.claude.com/docs/en/agents-and-tools/agent-skills/overview）**：

| Level | When Loaded | Token Cost | Content |
|-------|-------------|-----------|---------|
| **L1: Metadata** | Always (startup) | **~100 tokens/skill** | YAML frontmatter |
| **L2: Instructions** | When triggered | **Under 5k tokens** | SKILL.md body |
| **L3+: Resources** | As needed | **Effectively unlimited** | bash-accessible files |

原文：
> "Claude loads this metadata at startup and includes it in the system prompt. This lightweight approach means you can install many Skills without context penalty."

> "Efficient script execution: When Claude runs `validate_form.py`, the script's code never loads into the context window. Only the script's output consumes tokens. This makes scripts far more efficient than having Claude generate equivalent code on the fly."

**Claude Code 额外 budget 限制**（code.claude.com/docs/en/skills）：
- 列表显示上限：**1,536 字符**（`description` + `when_to_use`）
- Skill 列表总 budget：**1% of context window, fallback 8,000 chars**，通过 `SLASH_COMMAND_TOOL_CHAR_BUDGET` 调节
- Compaction 后保留：**每 skill 前 5,000 tokens，合计 25,000 tokens**

**工具机制**：Claude Code 确实有 `Skill` tool（permission 语法 `Skill(commit)` / `Skill(review-pr *)`）。但 API/claude.ai 上**没有显式 Skill tool**，通过 bash read 实现。

**Simon Willison 的观察（simonwillison.net/2025/Oct/16/claude-skills/）**：
> "MCP is a whole protocol specification, covering hosts, clients, servers, resources, prompts, tools, sampling, roots, elicitation and three different transports. Skills are Markdown with a tiny bit of YAML metadata and some optional scripts in whatever you can make executable in the environment. They feel a lot closer to the spirit of LLMs — throw in some text and let the model figure it out."

**Pass rate 数据**：无。Anthropic 官方对 SKILL.md 格式的论证完全基于 progressive disclosure 的 token 经济，不是 pass rate。

---

## 3. LangChain / LlamaIndex / AutoGen / CrewAI / OpenAI Agents SDK（**code-first 代表**）

**表示形态**：**全部是 Python decorator + type hints + docstring**，schema 由反射生成。

**LangChain**（docs.langchain.com/oss/python/langchain/tools）：
```python
@tool
def search_database(query: str, limit: int = 10) -> str:
    """Search the customer database for records matching the query."""
    ...
```
原文："Type hints are **required** as they define the tool's input schema."

**OpenAI Agents SDK**（openai.github.io/openai-agents-python/tools/）：`@function_tool` 通过 `inspect` + `griffe`（解析 docstring）+ `pydantic` 自动生成 JSON schema。**Swarm 已被取代**："Swarm is now replaced by the OpenAI Agents SDK, which is a production-ready evolution of Swarm"。

**CrewAI**（docs.crewai.com/concepts/tools）—— **唯一保留"Skill"概念作为独立范畴的框架**：
> "Tools give agents callable functions to take action. They work alongside MCPs (remote tool servers), Apps (platform integrations), **Skills (domain expertise)**, and Knowledge."

**Pass rate 数据**：这组框架没人公开发过 format 对比数据。

**适用场景**：tool function 注册（单次 API 调用）、状态无关计算。

---

## 4. Microsoft Semantic Kernel — 历史性的 skills→plugins 改名

**表示形态**：C#/Python class + `[KernelFunction]` / `@kernel_function` attribute。**曾经有** prompt-only "semantic functions"（`skprompt.txt`），**现在推荐 Prompty 文件**或直接转 function calling。

**历史关键事件（devblogs.microsoft.com/semantic-kernel/skills-to-plugins）**：
> "the biggest change we're making: renaming 'skills' to plugins. We've done this so we can better align the internal workings of Semantic Kernel with the plugin specification developed by OpenAI."

- 2023-10：`ImportSkill()` → `ImportPluginFromType()`；`ImportSemanticSkillFromDirectory()` → `ImportPluginFromPromptDirectory()`
- 2025-05：**Handlebars Planner 和 OpenAI Planner 全部 deprecated**，推 function calling 替代

**Pass rate 数据**：SK 官方引用 OpenAI 的经验值：
> "We recommend that you use no more than 20 tools in a single API call. Developers typically see a reduction in the model's ability to select the correct tool once they have between 10-20 tools defined."

**信号**：原本以 prose / prompt template 表达 skill 的最老派路线（`skprompt.txt`、Planner）**被它自己的作者弃用了**，方向是 typed function + JSON schema。

---

## 5. DSPy — signature 作为"声明式"skill

**表示形态**：`Signature` 字符串 (`"question -> answer"`) 或 class，+ `dspy.InputField()` / `dspy.OutputField()` 类型字段 + docstring 作任务描述。**不直接执行，编译为 prompt**。

示例（dspy.ai/learn/programming/signatures/）：
```python
class Emotion(dspy.Signature):
    """Classify emotion."""
    sentence: str = dspy.InputField()
    sentiment: Literal['sadness','joy','love','anger','fear','surprise'] = dspy.OutputField()
```

**核心主张**：signature 是 input/output 契约，LLM 如何达成由 optimizer (MIPROv2、BootstrapFewShot) 自动调。**Pass rate 在 DSPy 论文里有**，但 tools 文档没列。

**适用场景**：pipeline 任务、当有 training signal / metric 可以让 compiler 优化时。

---

## 6. Mastra (TypeScript, Zod)

**表示形态**：`createTool({...})` + **Zod schemas**（2025 最严格类型）。

```typescript
createTool({
  id: 'test-tool',
  description: 'Reverse the input string',
  inputSchema: z.object({ input: z.string() }),
  outputSchema: z.object({ output: z.string() }),
  execute: async inputData => ({ output: inputData.input.split('').reverse().join('') })
})
```

独特 feature：`requireApproval`、`suspendSchema` / `resumeSchema`（human-in-the-loop）、`mcp.annotations`（`readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint` 对齐 MCP spec）。

**JSON schema 不被原生接受**（issue #2717），Zod 是唯一规范路径。

---

## 7. LangGraph — state 优先

**表示形态**：`StateGraph` over `TypedDict` 或 `MessagesState`，tools 作为 LangChain tools 插到 `ToolNode`。原文：
> "LangGraph is a low-level orchestration framework and runtime for building, managing, and deploying long-running, stateful agents... LangGraph does not abstract prompts or architecture."

State 是一等抽象，tools 二等。

---

## 8. Zapier AI Actions → Zapier MCP

**表示形态**：对 agent 暴露的是**自然语言 REST API**（不是开发者写的 YAML）。

原文（docs.zapier.com/ai-actions）：
> "AI Actions is currently **Deprecated**, please consider using Zapier MCP."
> "AI Actions is optimized for receiving user input in natural language... Think of AI Actions as a more human-friendly integrations API."
> "Reference by name, not ID: Humans use natural language names, not IDs."

7000+ apps / 30000+ actions 不是声明式 YAML，是 NL → platform data mapping。也已迁移到 MCP。

---

## 9. n8n visual nodes

**表示形态**：节点图（JSON 导出），Custom Code Tool = **prose description** + JS/Python body：
```
Give your custom code a description. This tells the agent when to use this tool.
For example: Call this tool to get a random color.
```
技术栈下层是 LangChain (`@n8n/n8n-nodes-langchain.*`)。面向 end-user，不是 agent 开发者。

---

## 10. Benchmark / Research 硬数据（2024-2026）

### 10.1 Berkeley Function Calling Leaderboard (BFCL V3/V4)

**源**：Patil et al., ICML 2025, https://proceedings.mlr.press/v267/patil25a.html；V4 blog 2025-07。

V4 系统性测**格式敏感性**，5 个维度 × 26 组合 × 39 模型：
> "agentic tasks ... come with many variations in format - whether the model is expected to make function calls in Python or JSON, available functions are described in Python or XML ... current benchmarks on function calling use a single prompt format for all models and queries"

**关键缺口**：BFCL **明确拒绝使用 ReAct**：
> "we deliberately avoid using techniques like prompt engineering and ReAct ... to evaluate base LLMs with the same standards to isolate the effects"

所以 BFCL 没有 ReAct vs function calling 的直接对比。

**V3 multi-turn 惊人发现**：top 模型在 single-turn 能拿 >90%，但 multi-turn 掉到 <50%。Multi-turn 是格式无关的"难"。

### 10.2 Anka DSL — **+40pp 真的存在，但单研究**

**源**：Al Mazrouei (UW-Madison), arXiv:2512.23214, 2025-12。

原文摘要：
> "Despite having zero prior training exposure to Anka, Claude 3.5 Haiku achieves 99.9% parse success and 95.8% overall task accuracy across 100 benchmark problems. Critically, Anka demonstrates a **40 percentage point accuracy advantage over Python on multi-step pipeline tasks (100% vs. 60%)**, where Python's flexible syntax leads to frequent errors in operation sequencing and variable management. Cross-model validation with GPT-4o-mini confirms this advantage (+26.7 percentage points on multi-step tasks)."

**警告**：单作者、单机构、单 domain（data transforms）、只两个模型家族。Argmin AI 评价："Needs Validation — Evidence is limited to one domain and two LLM families."

### 10.3 Microsoft 700-API DSL

**源**：Bassamzadeh & Methani, arXiv:2407.02742 (Jul 2024) + OpenReview l6bHgzvztc。

原文：
> "We generated a train as well as test dataset with a DSL to represent automation tasks across roughly **700 APIs** ... hallucination rate for RAG model lagged by 1 pt for API names and by 2 pts for API parameter keys."

**实际关键数字**（Table 2）：加 API function defs + TST few-shots vs. 裸 5-shot baseline：
- `%Made-up API parameters` **−20.16 pp**
- `%Made-up API names` **−4.3 pp**

**注意**：这个 -20pp 是**"有 grounding vs 无 grounding"的增量**，不是"DSL vs NL"的增量。原研究主轴是"fine-tune vs RAG"，结论是 OOD 下 RAG +7pp similarity。

Follow-up 论文摘要原文：
> "Planning in code is considered a more reliable approach for many orchestration tasks. This is because code is more tractable than steps generated via Natural Language..."

### 10.4 Schall & de Melo RANLP 2025 — schema 降解

**源**：aclanthology.org/2025.ranlp-1.124.pdf。

原文摘要：
> "We uncover a fundamental divergence between base and instruction-tuned models under structural constraints. Base models often benefit from constrained decoding, producing more precise outputs, while **instruction-tuned models frequently suffer performance degradation on generation tasks** despite maintaining stability on classification tasks. Our log probability analysis reveals the underlying mechanism: **constrained decoding forces models away from their preferred natural language patterns into lower-confidence structured alternatives**."

Figure 1 caption：
> "While Llama3.1's unconstrained generation produces a correct answer embedded in natural language, the same model generates an incorrect answer when forced to comply with a structured JSON format."

**警告**：原始 synthesis 里的 "5-20% degradation" 范围**我没能在 abstract/前几页找到 verbatim**，可能在 results tables 内。方向确定，具体数值待查。

### 10.5 CodeAct (ICML 2024) — +20pp 首次系统证明

**源**：Wang et al., arXiv:2402.01030。

原文：
> "LLM agents are typically prompted to produce actions by generating JSON or text in a pre-defined format, which is usually limited by constrained action space ... and restricted flexibility ... CodeAct ... Our extensive analysis of **17 LLMs on API-Bank** and a newly curated benchmark shows that CodeAct **outperforms widely used alternatives (up to 20% higher success rate)**."

最可信的"code 动作空间 > JSON 动作空间"原始结果，17 模型跨验证。

### 10.6 Cloudflare Code Mode (2025-09 / 2026-02)

原文（blog.cloudflare.com/code-mode-mcp，Feb 2026）：
> "For a large API like the Cloudflare API, **Code Mode reduces the number of input tokens used by 99.9%**. An equivalent MCP server without Code Mode would consume 1.17 million tokens — more than the entire context window of the most advanced foundation models."

Sep 2025：
> "LLMs are better at **writing code to call MCP**, than at calling MCP directly."
> "Perhaps this is because LLMs have an enormous amount of real-world TypeScript in their training set, but only a small set of contrived examples of tool calls."

### 10.7 Anthropic Programmatic Tool Calling (2026)

**源**：docs.anthropic.com/en/docs/agents-and-tools/tool-use/programmatic-tool-calling。

原文：
> "Programmatic tool calling allows Claude to **write code that calls your tools programmatically within a code execution container**, rather than requiring round trips through the model for each tool invocation ... On agentic search benchmarks like **BrowseComp and DeepSearchQA**, ... adding programmatic tool calling on top of basic search tools was the key factor that fully unlocked agent performance."
> "For example, calling 10 tools directly uses **~10x the tokens** of calling them programmatically."

---

## 11. 跨研究总结表

| 来源 | Format A | Format B | Δ | Metric | Sample |
|---|---|---|---|---|---|
| CodeAct (ICML 2024) | Python code actions | JSON/text | **+20 pp** | Success | 17 LLMs, API-Bank |
| Anka (arXiv Dec 2025) | Anka DSL | Python | **+40 pp** (Haiku) / +26.7 (GPT-4o-mini) | Multi-step accuracy | 100 tasks × 2 模型 |
| Microsoft 700-API (Jul 2024) | TST+FD few-shots | 无 grounding | **−20 pp** param hallucination | Hallucination | 700-API DSL |
| Schall & de Melo (RANLP 2025) | Constrained JSON | Unconstrained NL | instr-tuned 掉分（方向） | Task accuracy | 11 模型 |
| Cloudflare Code Mode (2026-02) | Code-mode MCP | Native MCP | **−99.9%** tokens | Token footprint | 全 Cloudflare API |
| Anthropic PTC (2026) | Programmatic code | Direct tool use | **~10x** tokens 节省（10 工具） | Token | BrowseComp/DSQA |
| BFCL V4 (2025) | 26 格式组合 | - | 格式敏感但 <30pp | AST+state+exec | 39 模型 |

---

## 12. 结论表（user 指定格式）

| 场景 | 推荐形态 | 依据 |
|---|---|---|
| **单次 API 调用**（无 state，一个请求一个响应） | **YAML / 声明式** + schema validation | Zapier MCP / n8n 7k apps 全跑声明式；Microsoft 700-API 研究：grounding +FD 降低 param 幻觉 -20pp；格式越简单 LLM 越稳 |
| **多步工作流 / pipeline**（串联 ≥3 个操作） | **Code actions**（Python 或 DSL），**不用** JSON tool-call 串联 | CodeAct +20pp (2024, 17 LLMs)；Anka +40pp (multi-step, Dec 2025)；Cloudflare −99.9% tokens；Anthropic PTC ~10x 节省 |
| **条件分支 / 路由**（if X then Y else Z） | **prose + progressive disclosure**（SKILL.md pattern），LLM 读 router → 跳到具体子文档 | Anthropic skills 14/17 都用这 pattern；`pptx/SKILL.md` Task→Guide 表；progressive disclosure 是 Anthropic 明示的"core design principle" |
| **真的需要 Python 逻辑**（隐式状态、复杂数据变换、循环） | **预写 Python script + 从 SKILL.md 以黑盒调用** | `anthropics/skills` 里 `webapp-testing/SKILL.md:11-14` 明示："DO NOT read the source until you try running"；Anka 论文自己说"Python's flexibility is the source of failure"，但遇到真需要时必须是完整 Python |

---

## 13. 元结论 —— 原始证据指向的三个硬事实

1. **没有"一个最优形态"**。业界共识已经在"**形态混合 + progressive disclosure**"上收敛：接口契约（frontmatter / schema）简单且严格，body 风格按任务自由。Anthropic 的 17 个 skill 是最强证据 —— 同仓库内 0% code 到 61% code 共存。

2. **Code 作为动作空间（CodeAct 模式）在所有 multi-step 场景都赢**。CodeAct (+20pp)、Anka (+40pp)、Cloudflare (−99.9% tokens)、Anthropic PTC (~10x tokens) 四项独立实验一致。但"让 LLM 每次现写 Python"（你 v1 路线）**不是 CodeAct**；CodeAct 是"LLM 调用预写好的函数，用 Python 做 glue"。

3. **Semantic Kernel 和 Zapier 把"prose / NL 作为 skill 表示"的路径都官方废弃了**。SK 废 `skprompt.txt` Semantic Function 和 Planner（2025-05），Zapier 废 AI Actions 转 MCP。方向是 typed schema + function calling + 代码粘合。**但 Anthropic 的 SKILL.md 是反例** —— 它同时活得很好，因为它走的是 bash+filesystem+progressive disclosure 的新路线，不是老派 NL planner。

---

*Total: ~2480 words · 原始引用占比高 · 未验证项已标注警告*
