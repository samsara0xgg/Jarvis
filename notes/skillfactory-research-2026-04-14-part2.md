# SkillFactory 调研 Part 2 — Multi-CC 编排 + CLI 多轮 + Debate

*Date: 2026-04-14 · 接 `skillfactory-research-2026-04-14.md`*

**这批回答的问题**：如何让两个 CC 协作 / 监督 / refine？技术上怎么做？理论上靠谱吗？

---

## TL;DR — 三条颠覆性发现

### 1. 生产界已有完整参考实现：EveryInc `compound-engineering-plugin`（14K stars）

**这是迄今最成熟的多-CC 互审生产方案**。其 `/ce:review` skill **并行 dispatch 25-35 个 reviewer sub-agent**（security / correctness / maintainability / adversarial / tests / ...），每个返回**结构化 JSON**（severity P0-P3 + autofix_class + confidence + owner），再由 synthesizer 合并去重。

支持 `mode:headless` → **skill-to-skill 可程序化调用**（你 SkillFactory 可以把它当黑盒用）。

### 2. CC 同模型互评 = 学术否定 + 生产界不用

**Huang 2024 (ICLR) "LLMs Cannot Self-Correct Reasoning Yet"**：GPT-4 无 oracle 信号下自我修正，GSM8K 95.5% → 91.5% → 89.0%（**单调下降**）。**Olausson 2024 ICLR**：成本调整后 self-repair 相对 i.i.d. 重采样只 0.97-1.09x，**经常 break-even**。**Wynn 2025**：往强 agent 的辩论里引入弱 agent，**反而拉低强 agent 性能**。

**Anthropic 自家工程博客（Jun 2025）承认**："multi-agent research uses ~15× more tokens than single-chat" 且明言 *"most coding tasks involve fewer truly parallelizable tasks than research"* —— **Anthropic 自己不对代码用多-agent，只对 research 用**。

成功生产案例（`hamelsmu/claude-review-loop`）的窍门是：**用 Codex 做 critic，不用第二个 Claude** —— 必须跨模型才能避开 self-enhancement bias。Vallecillos-Ruiz 2025 证据：diversity-based 两模型 ensemble 达到 oracle 上限的 95%。

### 3. CC CLI 多轮已经是 first-class，你**不需要手搓会话管理**

你现在的 `subprocess.Popen(["claude", "-p", ...])` 是 one-shot 模式，每次调用零上下文。事实上 CC CLI 支持的机制：

- 会话文件自动存在 `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`
- `claude -p "$p1" --output-format json | jq -r '.session_id'` 捕获 session_id
- `claude -p "$p2" --resume "$session_id"` 接续上下文
- `--fork-session` 分支而不丢原 session
- Python SDK `ClaudeSDKClient` 完全封装了这些（`async with ... as client: await client.query(...)` 自动续）

**推荐 SDK 而不是裸 CLI 链式**：单 session 自动保 cache + 自动管 context。

---

## Part 2.1 — 多-CC 编排（Agent D）

### 生产工具矩阵

| 工具 | 真实架构 | 值得学 |
|---|---|---|
| **EveryInc compound-engineering-plugin** (14K★) | `/ce:review` 并行 25-35 reviewer sub-agent，结构化 JSON，synthesizer 合并，支持 headless | **最成熟的参考实现**，直接研究源码 |
| **hamelsmu/claude-review-loop** (638★) | **Claude × Codex**（跨厂商！）。Stop hook 拦 CC 退出 → 2 阶段 lifecycle (`task` → `addressing`) → Codex 并行 4 sub-agents 写 `reviews/review-<id>.md` | 最干净的跨模型互评模式，hook 技术值得抄 |
| **tomasz-tomczyk/crit** (122★) | 人在环 UI：CC 编辑 → 浏览器显示 PR 风格 inline comments → `Send to agent` 按钮 pipe 回 CC stdin | 你的 SkillFactory 场景不需要（你要全自动） |
| **ruvnet/claude-flow** | 不是多-CC process，而是 in-process 角色 + Task tool + AgentDB | 过度设计，跳过 |
| **anthropics/claude-code-action** (7K★) | Git diff pipe 到 CC，官方 GitHub Actions 集成 | Git-based handoff 模式参考 |

### 架构模式评分（按你的 SkillFactory 适配度）

| 模式 | 例子 | 评分 | 原因 |
|---|---|---|---|
| **Generator + Rubric Critic**（单轮） | hamelsmu/claude-review-loop | ★★★★★ | 简单、有界、单文件 handoff。你已经 80% 到位 |
| **Committee review**（并行多维度 reviewer） | EveryInc /ce:review | ★★★ | 维度覆盖好但贵（25 reviewer × $0.005） |
| **Planner + Executor + Reviewer** | EveryInc /ce:plan → /ce:work → /ce:review | ★★ | 对 skill 生成过度，planning 近乎 trivial |
| **Debate + Adjudicator** | 学术为主 | ★ | 没生产 CC 实现，skip |
| **Pipeline via git**（worktree 隔离） | claude-code-action | ★★★ | worktree 并行是好模式但你不需要并行 |

### 两个 CC 怎么通信 — 实操机制

**① `subprocess.Popen + --bare --output-format json`** — first-class 支持。你已经在用 `Popen`，只需加 `--bare`（跳过 hook/MCP 自动发现，确定性）+ JSON 输出。
- `--allowedTools "Read,Edit,Bash(pytest *)"` 门控工具
- `--append-system-prompt` 钉住角色
- 启动开销 ~1-2s/次

**② `--output-format stream-json --verbose`** — NDJSON 事件流（`tool_use`/`tool_result`/`assistant`/`result`），适合实时进度 + 日志。

**③ Filesystem handoff** — CC A 写 `reviews/review-<id>.md`，hook 验存在才放行。**生产界首选**，能存盘可调试可复跑。

**④ Git-based** — 分支 / worktree 隔离；`gh pr review` 评审。

**⑤ MCP server as shared state** — 过度工程，skip。

**⑥ `--continue` / `--resume <session_id>`** — 同一 CC 续 session。

### Claude Code 自家原生多-agent 工具

- **Task tool / subagents** — same-process 隔离上下文，`run_in_background:true` 并行，**最便宜最快**的 Generator + Critic 实现路径
- **`.claude/agents/*.md`** — 自定 sub-agent，frontmatter 声明 `tools` + `model`（Haiku 做 critic 省钱）
- **Stop hook** — **review loop 的杀手级 primitive**（claude-review-loop 核心技术）
- **SubagentStop hook** — Task-tool worker 结束时触发

**优先顺序**：(1) `.claude/agents/` + Task tool for critic → (2) Stop hook + subprocess for 第二 CC 隔离 → (3) SDK 做富控制。

### 失败模式 + 缓解

| 失败模式 | 缓解 |
|---|---|
| 同模型 echo chamber | **换模型**（Codex/GPT-5/Gemini）或 rubric-driven binary pass/fail |
| 无限循环 | `max_rounds=2-3` 硬顶，sentinel 文件（`RESOLVED:` 前缀） |
| 成本飙升 | Haiku 做 critic；strict rubric 单轮；cache via `--resume` |
| Hook 死锁 | 30s hook 超时 + `trap ... ERR` fail-open |
| Pre-existing issue 噪音 | rubric 里 `pre_existing: bool` 字段区分 |
| 沙箱污染 | git worktree per CC；`mode:report-only` |

### 推荐 SkillFactory 具体改造

你 `core/skill_factory.py` 已经用 `subprocess.Popen` + regex scan + pytest，**80% 到位**。加：

```python
# 1. Generator（现有 + --bare + JSON）
gen_result = subprocess_run_cc(
    prompt=gen_prompt,
    extra=["--bare", "--output-format", "json",
           "--allowedTools", "Read,Write,Edit",
           "--append-system-prompt", "You write secure Python skills..."])
session_id = gen_result["session_id"]

# 2. 确定性 gate（保留现有）
dangerous_scan(skill_path)  # regex
pytest_run(skill_path)
signature_check(skill_path)

# 3. Rubric Critic（Haiku 省钱，JSON schema 强制结构）
RUBRIC_SCHEMA = {
    "imports_skill_base": "bool",
    "has_type_hints": "bool",
    "handles_errors": "bool",
    "follows_config_yaml": "bool",
    "verdict": "approve|revise",
    "issues": [{"severity": "P0-P3", "line": "int", "fix": "str"}]
}
critic_result = subprocess_run_cc(
    prompt=critic_prompt,
    extra=["--bare", "--model", "haiku",
           "--output-format", "json",
           "--json-schema", json.dumps(RUBRIC_SCHEMA),
           "--allowedTools", "Read"])

# 4. 最多一次 revision round
if critic_result["verdict"] == "revise":
    revise_result = subprocess_run_cc(
        prompt=revise_prompt,
        extra=["--resume", session_id])  # 复用 gen 的上下文
    # 再 gate；还不过 → reject 不循环
```

**成本估算** per skill：Sonnet generator ~$0.02 + Haiku critic ~$0.001 + 1 revision ~$0.015 = **$0.035，带 1 revision 预算 $0.08**。

**绝对别用**：claude-flow swarms、Byzantine consensus、MCP-IPC。生产工具底层都是 `subprocess.Popen + JSON + filesystem`。

---

## Part 2.2 — CC CLI 多轮机制（Agent E）

### Session 机制揭秘

```bash
# 会话文件位置
~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
#           ↑ 把你的 cwd 所有非字母数字字符替换为 -
# 例：/Users/allen/Projects/jarvis → -Users-allen-Projects-jarvis

# 跨机器不能用 —— encoded-cwd 必须完全一致
```

三种续接方式：

```bash
# 1. 续当前 cwd 最近的 session（不用追踪 id）
claude -p "现在审核它" --continue

# 2. 指定 session 续
claude -p "再改一版" --resume "550e8400-e29b-41d4-a716-446655440000"
claude -r "skill-gen-001" "..."       # 用 --name alias

# 3. 预分配 UUID（程序化幂等性）
claude -p --session-id 550e8400-... "开始"
```

**SkillFactory 典型 3 轮模式（exact pattern）**：

```bash
# Round 1: 生成并抓 session_id
session_id=$(claude -p "写一个查汇率的 Skill" \
    --output-format json | jq -r '.session_id')

# Round 2: 审核（full 前文可见）
claude -p "审核刚写的 skill：安全/错误处理/边界" --resume "$session_id"

# Round 3: 修
claude -p "根据审核建议修复" --resume "$session_id"

# 分支（不丢原 session）
claude -p "同时写个 TS 版本" --resume "$session_id" --fork-session
```

### Output 格式 JSON 内容

```json
{
  "type": "result",
  "subtype": "success",
  "result": "生成的内容...",
  "session_id": "380bd0cd-...",
  "duration_ms": 3216,
  "num_turns": 1,
  "total_cost_usd": 0.0159,
  "usage": {
    "input_tokens": 3,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 14253,
    "output_tokens": 8
  }
}
```

解析：`jq -r '.result'` → 最终文本；`.session_id` → 续用；`.total_cost_usd` → 成本监控。

强制 schema：`--json-schema '{"type":"object",...}'`，读 `.structured_output`。

### Stream-json 事件总线（实时驱动）

`--output-format stream-json --verbose` → NDJSON：

| type | 时机 | 关键字段 |
|---|---|---|
| `system` (init) | 开头，必须 | `session_id`, `model`, `tools`, `cwd` |
| `assistant` | 每次响应 | `message.content[]`（text / tool_use） |
| `user` | tool result 回显 | tool-result content |
| `stream_event` | 仅 `--include-partial-messages` | `event.delta.text`（token 级） |
| `rate_limit_event` | 每 API 调用 | `rate_limit_info.status` |
| `result` | 末尾，必须 | `result`, `total_cost_usd`, `num_turns` |

**`--verbose` 必须和 `stream-json` 配对**，否则 init 事件可能掉，拿不到 session_id。

### 跨-CC stdin/stdout 管道

```bash
# 第一个 CC 分析，第二个 CC 总结
claude -p "分析 auth.py" --output-format stream-json --verbose | \
  claude -p "总结分析" \
    --input-format stream-json --output-format stream-json --verbose
```

### SDK vs 裸 CLI — 何时用哪个

**用 Python Agent SDK（`ClaudeSDKClient`）写 SkillFactory loop driver**，不要裸 CLI 链式：

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage

async def build_skill(spec: str, max_rounds: int = 5):
    opts = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="acceptEdits",
        setting_sources=["project"],      # 加载 CLAUDE.md
        cwd=SKILL_DIR,
        max_turns=30,
    )
    async with ClaudeSDKClient(options=opts) as client:
        rounds = [
            f"写一个实现的 Skill: {spec}",
            "审核刚写的 skill，输出 audit.md",
            "修 audit.md 里的问题，重写 audit_v2.md",
            "写 pytest 跑到全绿（max 3 次）",
            "输出 report.json {status, tests_passing, issues}",
        ]
        for i, prompt in enumerate(rounds[:max_rounds]):
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage) and msg.subtype != "success":
                    raise RuntimeError(f"Round {i} failed")
    return json.loads(open(f"{SKILL_DIR}/report.json").read())
```

**为啥 SDK 赢**：
- 单 client = 自动 session 续 + cache 友好
- Filesystem 作 truth（`audit.md`, `report.json`）熬过 session 截断
- `acceptEdits` + `max_turns=30` = 自主但有界
- Sentinel file = Ralph-style 停止条件，不用 bash loop
- `setting_sources=["project"]` = 自动加载你的 CLAUDE.md 规范

### Hook 的能力边界（重要）

Hook 事件：`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`SubagentStop`、`SessionEnd`。

Hook **能**：block action（`{"decision":"block"}`）、注入 context（`additionalContext`）、改 tool 输入、halt。

Hook **不能**：spawn 新 CC process、强制 follow-up prompt。**所以 refine loop 的 orchestrator 必须在外部**（Python SDK 或 bash while）。`Stop` hook 返 `{"decision":"block"}` 会让**同一 turn** 继续，不开新 session。

### 致命 Gotcha 汇总

1. **`--verbose` 和 `stream-json` 必须配对**
2. **cross-cwd resume = fresh session**（encoded-cwd 必须一致）
3. **Prompt cache 每次 `claude -p` 重置** —— `--resume` 会 replay 整个 transcript → `cache_creation_input_tokens` 飙高
4. **Issue #10161**：`--resume` 超 context 限额可**静默砍老轮次**
5. **Issue #42542**：1M 上下文 Opus 4.6 上 tool result 可能**静默丢**
6. **Context 最后 20% 质量下降** —— 1M 窗口下保工作状态 < 800k，用 `/compact` 或 fork
7. **`--bare` 跳过 CLAUDE.md/hooks/MCP** —— 生产脚本确定性首选，但要用 `--append-system-prompt-file` 补 project memory
8. **每次 resume re-send 全部 transcript** —— cache 命中 ~90% 折扣，但 `num_turns × 历史大小` 是成本缩放项
9. **Opus 4.6 1M context 还是 beta** —— `--model claude-opus-4-6` 显式指定，看 init 事件的 `model` 字段验证
10. **`--max-turns` 达到会 error 退出** —— 从 result 事件抓 session_id 可 resume 加大 turns

### 生产案例参考

- **Ralph Loop**（Geoffrey Huntley 的模式，现官方 plugin `ralph-wiggum@claude-plugins-official`）：bash `while` 反复调 CC 用相同 prompt 直到 sentinel 输出
- **`anthropics/claude-code-action`**（7K★）：GitHub Action，git + PR comments 做 state
- **Chained CC via stream-json**（AgentMastered）
- **`anthropics/claude-agent-sdk-demos`**：email assistant、research agent，全用 in-process session continuity

---

## Part 2.3 — Multi-Agent Debate for Code（Agent F）

### 学术共识（带 benchmark）

**多-agent debate 对 reasoning/factuality 有效**（Du 2023、Liang 2023 的 MMLU、GSM8K、翻译），**对代码生成几乎没有独立 debate 证据**。所有 "code multi-agent" 赢的系统本质都是 **"generator + test executor + refine loop"**，不是 debate。

| 系统 | 架构 | HumanEval | 备注 |
|---|---|---|---|
| GPT-4 baseline | 单 agent | ~80-88% | prompt 敏感 |
| Self-Collaboration (Dong) | analyst+coder+tester | 90.2% | 单模型多角色 |
| ChatDev | 7 agents SDLC | N/A | 做完整 app，TypeError 率高 |
| MetaGPT | 5 agents + SOP | 85.9% | test acc 只 ~79% |
| **AgentCoder** | programmer+test designer+executor | **96.3%** | **赢在 executable feedback，不是 debate** |

### LLM-as-Judge 的已知偏差（重要）

**Zheng 2023 MT-Bench** 确立的基准偏差：

- **Position bias**：GPT-4 评判对偶时**60%+ 偏好前者**
- **Self-enhancement bias**：同模型判自己输出 **~10% 抬分**
- **Length/verbosity bias**：长回答得高分（跟质量无关）
- **Sycophancy**：agents 会收敛到错误多数（Estornell 2025 "tyranny of the majority"）

**Yao 2025**：**disagreement rate 随 debate rounds 单调下降，伴随性能下降** —— 模型 sycophantically 趋同**即便答案错**。

**Tsui 2025 "Self-Correction Blind Spot"**：15+ 模型平均 **64.5% self-correction blind spot** —— 模型能看外部输入的错，但看不见自己完成的错。单纯在 prompt 后加 "Wait" 能把 blind spot 降 89.3%，不用 fine-tune —— 说明**失败是激活问题，不是知识问题**。

### Same-model vs Cross-model Review

**Huang 2024 (ICLR) "LLMs Cannot Self-Correct Reasoning Yet"**：GPT-4 在 GSM8K 上 standard 95.5% → self-correct round 1 **91.5%** → round 2 **89.0%**。**单调退化，没有 oracle feedback**。GPT-4-Turbo 同模式（91.5 → 88.0 → 90.0）。

**Wynn 2025 "Talk Isn't Always Cheap"**：**把较弱 agent 引入 debate → 拉低较强 agent 性能**。同模型 debate 在 MMLU/GSM8K 上小涨，在 CommonsenseQA 上**始终有害**。rounds 越长越退化。

**Vallecillos-Ruiz 2025 "Wisdom and Delusion"**：ensemble oracle 上限可以**高于最佳单模型 83%**，但 consensus 策略掉 "popularity trap"。**Diversity-based selection 在两模型 ensemble 就达到理论上限 95%**。强力支持跨模型。

### Multi-Agent **伤害**性能的场景

- **CommonsenseQA**：debate 始终有害（Wynn 2025）—— 无 verifier，agents 收敛到"合理但错"的共识
- **强 + 弱 mix**：弱的靠 sycophancy 拉低强的
- **无 oracle 的 self-refine**：Huang 2024 + Olausson 2023 —— 匹配算力后增益消失
- **成本飙升**：Anthropic 自己 **multi-agent research ~15× tokens**。Anthropic 工程博客（Jun 2025）：*"most coding tasks involve fewer truly parallelizable tasks than research, and LLM agents are not yet great at coordinating and delegating."* **他们自己不在代码上用 multi-agent**。

### Convergence 行为

- Du 2023 固定 2-3 轮，无自适应终止
- Liang 2023 有 Judge agent 做自适应 break —— 性能**先升后降**如果强制继续
- Yao 2025：disagreement 单调降 —— debate 靠 conformity 终结，不靠 truth-finding
- AgentCoder：5 轮最优，3 轮后递减
- **真正的失败是过早趋同，不是死循环**

### 理论极限

- Generator 和 Critic 共享训练数据 → **共享盲点持续**
- Huang 2024 推论一般化：**内生 self-correction 需要 (a) oracle 信号 或 (b) 严格更强的 critic**
- 代码里 oracle = 可执行测试 / 类型检查器 / fuzzer。**Debate 本身不是 oracle**
- Debate 对"可验证、多步、有陷阱的推理"最有效（CIAR 问题、翻译习语）
- Debate 对"事实召回"和"特异 API 用法"最无效 —— generator 知道就知道，不知道就不知道

### 对 SkillFactory 的最终结论

**"CC 写 → CC 审 → debate 到收敛"这个架构字面上是平庸的**。

但**可以改造为有效版本**：

- **Generator**：Claude Code
- **Critic**：**不同模型**（Codex / GPT-5 / Gemini 2.5 Pro）—— hamelsmu/claude-review-loop 就是这个模式，**不用 dual-Claude**
- **每轮强制 ground-truth 信号**：跑 pytest、type check、linter、fuzzer。**不允许纯文本 debate**
- **硬轮次上限 2-3 轮**，自适应 break on test-pass；强制终止防趋同崩溃
- **Rubric-structured critic prompt**（Level-2 建设性，非 Level-3 对抗性 —— 按 Liang 2023）
- **critic 和测试冲突时，以测试为准**。测试是 oracle，critic 是启发式

**如果你吝啬 cross-model 成本**：**同 CC 的 best-of-N + 执行过滤 在匹配 token 预算下往往胜过 same-model debate**（Olausson 2023、Huang 2024 有数据）。更简单、更好。

**何时 same-model debate 可以接受**：(a) 没可执行 verifier（spec 写作、架构审查）、(b) 任务 reasoning-heavy 不是 knowledge-heavy、(c) 接受 ~15× 成本换边际收益。**生产代码生成有测试时，是错工具**。

---

## Sources（Part 2，~35 个）

**Agent D — 多-CC 编排**：
- hamelsmu/claude-review-loop README + Stop hook bash 源码
- EveryInc/compound-engineering-plugin README + `/ce:review` SKILL.md
- tomasz-tomczyk/crit README + `git.go`
- ruvnet/claude-flow DeepWiki 文档
- parruda/claude-swarm V1/V2 `execution-flow.md`
- anthropics/claude-code-action `.github/workflows/claude-review.yml`
- code.claude.com/docs/en/headless
- code.claude.com/docs/en/subagents
- anthropics/claude-code issue #19220, #33682
- lossoffn.com "Compound Engineering for Claude Code"
- agentpatterns.ai Compound Engineering 文章
- dev.to "Postmortem on Autonomous LLM-as-Judge"（2026-04-08）
- anthropics/claude-agent-sdk-python `transport/subprocess_cli.py`

**Agent E — CC CLI 多轮**：
- code.claude.com/docs/en/cli-reference
- code.claude.com/docs/en/agent-sdk/sessions
- code.claude.com/docs/en/headless
- code.claude.com/docs/en/agent-sdk/overview
- code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
- code.claude.com/docs/en/hooks
- Roasbeef/claude-agent-sdk-go cli-protocol.md（逆向 stream-json schema）
- Agent Mastered "Streaming & Real-Time Output"
- Shipyard "The Ralph Loop"
- ClaudeLog "Tight Feedback Loops"
- anthropics/claude-agent-sdk-python README
- CC GitHub issues #10161, #24594, #24596, #35127, #42542

**Agent F — Multi-Agent Debate**：
- Du et al. "Improving Factuality and Reasoning through Multiagent Debate" arXiv:2305.14325（ICML 2024）
- Irving/Christiano/Amodei "AI Safety via Debate" arXiv:1805.00899
- Liang et al. "Encouraging Divergent Thinking" arXiv:2305.19118（EMNLP 2024）
- Qian et al. ChatDev arXiv:2307.07924
- Hong et al. MetaGPT arXiv:2308.00352
- Huang et al. AgentCoder arXiv:2312.13010
- Dong et al. Self-Collaboration arXiv:2304.07590
- Zheng et al. MT-Bench arXiv:2306.05685（NeurIPS 2023）
- Huang et al. "LLMs Cannot Self-Correct Reasoning Yet" ICLR 2024 arXiv:2310.01798
- Olausson et al. "Demystifying GPT Self-Repair for Code Generation"
- Wynn et al. "Talk Isn't Always Cheap" arXiv:2509.05396
- Tsui "Self-Correction Bench" arXiv:2507.02778
- Vallecillos-Ruiz/Hort/Moonen "Wisdom and Delusion" arXiv:2510.21513
- Anthropic Engineering "How we built our multi-agent research system"（Jun 2025）
