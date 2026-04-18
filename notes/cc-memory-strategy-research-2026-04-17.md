# CC 记忆管理策略深度研究 — 2026-04-17

> **研究范围**：Claude Code 生态截至 2026-04-17 的最优记忆管理策略
> **目标读者**：单人开发者（多项目并行 + Obsidian vault）
> **研究方法**：3 路并行 subagent + 40+ 独立 source（官方 docs / 学术论文 / 开发者 blog / GitHub issue）
> **主要空白**：claude-mem 独立 benchmark 缺失、Anthropic v2.1.50 memory 注入具体机制未完全公开

---

## 0. Executive Summary（3 分钟读完）

1. **CLAUDE.md ≠ auto-memory MEMORY.md** — 前者是**你写**给 Claude 的规则，后者是 Claude v2.1.59+ **自动维护**的笔记。两套并存，不是替代关系。
2. **CLAUDE.md 越短越有效** — 社区实测数据：<50 行 = 96% 合规 / <200 行 = 92% / >400 行 = **骤降到 71%**。ETH Zurich 论文：`/init` 自动生成的 CLAUDE.md 让 SWE-bench 掉 20%。
3. **MEMORY.md 有硬上限 200 行 / 25KB，超出内容静默丢弃不报警**。你现在 18 条已接近上限。
4. **auto-memory 和 claude-mem 功能重叠** — 两个都是 SessionStart 注入 + 跨 session 回忆。加上 Anthropic 在 v2.1.50 后把 user memory 从 system prompt 移除（Cisco 2026-04-01 报道的 memory poisoning 攻击后果），claude-mem 的生态路径正在变不稳。
5. **多 MCP server 是 context 吸血鬼** — GitHub MCP 单个 55K tokens，多 server setup 总计 15–20K/turn，占 200K 窗口 ~10%。
6. **"manage 步骤是行业最被忽略的环节"**（Nick Lawson TDS 2026-04-17）— 大多数人做了 write + read，唯独不做 prune。
7. **对你的推荐**：**删 claude-mem**、**保留 auto-memory（精简 < 150 行）**、**Obsidian 走文件系统桥接而不是 MCP**、**MCP servers 做一次审计**。

---

## 1. Claude Code 官方记忆机制（权威事实）

### 1.1 两套不同系统

| 维度 | CLAUDE.md | auto-memory MEMORY.md |
|------|-----------|----------------------|
| 作者 | 你手写 | Claude 自动生成 |
| 内容 | 指令、规则、约定（"always do X"） | 学习、模式、决策（"之前发现 Y"） |
| 作用域 | 项目 / 用户 / 组织 | 单项目（按 cwd hash） |
| 加载 | 每次 session 启动完整加载 | 仅前 200 行 / 25KB 启动加载 |
| 启用条件 | 默认 | v2.1.59+ 默认开启 |

> Source: [code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory)

### 1.2 CLAUDE.md 优先级层级

```
1. 组织 CLAUDE.md （/Library/Application Support/ClaudeCode/CLAUDE.md）
2. 项目 CLAUDE.md （./CLAUDE.md 或 .claude/CLAUDE.md，入 git）
3. 用户 CLAUDE.md （~/.claude/CLAUDE.md，所有项目）
4. 本地 CLAUDE.md （./CLAUDE.local.md，入 .gitignore）
5. 子目录规则 （.claude/rules/*.md，按路径匹配加载）
```

### 1.3 auto-memory 机制细节

- 存储：`~/.claude/projects/<project-hash>/memory/` 目录下 Markdown 文件
- 索引：`MEMORY.md`（你看到的那个）
- 内容：typed markdown 文件（user / feedback / project / reference 四类是社区约定）
- 注入方式：作为 **user message**（不是 system prompt），v2.1.50 后变更 ← **这点很关键**
- 管理：`/memory` 命令 · `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` env var 禁用

### 1.4 Memory Tool API（API 层，跟 CC CLI 不同）

- `memory_20250818` tool 是 Anthropic 2025-09 发布的 API 层能力
- 命令：view / create / str_replace / insert / delete / rename
- 由开发者实现后端（文件 / DB / 云）
- **Claude Code CLI 不直接暴露这个 tool**，而是用自己的 auto-memory 系统包装

> Source: [platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)

### 1.5 Sessions & Teleport

- 本地 session：`~/.claude/projects/<project>/*.jsonl`
- 云端 session（Web/Desktop 创建的）：`claude --teleport` 或 `/teleport` 拉到本地
- **没有官方跨 session 召回 API** — 要回看历史只能用第三方（claude-mem）或自己搜 `.jsonl`

---

## 2. Memory MCP / Plugin 生态（2026-04 全景）

### 2.1 产品矩阵

| 名称 | 存储 | 集成方式 | 自动/手动 | 活跃度 | 备注 |
|-----|-----|---------|---------|---------|-----|
| **claude-mem** (thedotmack) | SQLite + Chroma + FTS5 | CC plugin + 5 hooks + 4 MCP tools | **全自动** | v12.1.6 @ 2026-04-16（极活跃但近期 regression 多） | CC 原生深度绑定 |
| **mem0 / OpenMemory** | Qdrant + optional Neo4j | MCP server | 半自动（LLM 调用） | v1.0.8 @ 2026-03-26 | LoCoMo 91.6%，跨 app 通用 |
| **basic-memory** (basicmachines-co) | Markdown + SQLite | MCP | 手动 | v0.20 活跃 | **Obsidian 原生兼容** |
| **supermemory** | SaaS hybrid | plugin + MCP 双模 | 全自动 | 2026-01 发 CC plugin | 付费托管 |
| **MCP Memory Server**（官方） | JSON flat 知识图 | MCP | 手动 | 2026-01-26 | 功能最基础 |
| **memento-mcp** | Neo4j + vector | MCP | 手动 | **2025-05 后无更新** | ❌ 维护停滞 |
| **superpowers** (obra) | 无记忆存储 | plugin + skill | N/A | v5.0.7 @ 2026-03-31 | **不是记忆插件**，是工作流方法论 |
| **Letta / Zep / Graphiti** | 时序 KG / 企业级 | SDK / MCP | 半自动 | 持续活跃 | 对单人开发者过重 |

### 2.2 关键洞察

1. **"MCP 触发不确定"是公认痛点** — supermemory 和 claude-mem 都放弃纯 MCP，改用 plugin hooks 强制触发。
2. **hybrid（向量 + 图 + FTS）压过纯向量** — 2026 所有新版本都在加图层 / temporal 层。
3. **本地优先派 vs 云托管派** — basic-memory / byterover 本地；mem0 SaaS / supermemory 托管。
4. **Anthropic 自己最小化支持** — 官方推荐 CLAUDE.md 为主路，外置 memory 是补充。
5. **Letta v1 agent 架构**（2025-10）代表"single super-agent + tiered memory"路线，但对多项目编码场景过重。

> Sources: [github.com/thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) · [mem0.ai/blog/claude-code-memory](https://mem0.ai/blog/claude-code-memory) · [basicmachines.mintlify.app/integrations/obsidian](https://basicmachines.mintlify.app/integrations/obsidian) · [letta.com/blog/letta-v1-agent](https://www.letta.com/blog/letta-v1-agent)

---

## 3. 多系统并行的反模式

### 3.1 Context 膨胀（最严重）

- **MCP tool schema 每 turn 重新注入** — 不缓存。多 server setup 15–20K tokens/turn，GitHub MCP 单个就 55K。
- 社区用 lazy-loading proxy（`lazy-mcp` / `mcp-tool-search`）可省 **95% tokens**（~15K → ~800）。
- **CLAUDE.md 长度曲线**：<50 行 → 96% 合规 · <200 行 → 92% · >400 行 → **71%**（骤降）。
- **ETH Zurich 2026-02 arXiv 2602.11988**（SWE-bench 300 任务）：`/init` 自动生成的 CLAUDE.md 让成功率 **下降 ~20%**；人工写的 +4%。

> Sources: [MindStudio 2026-04-02](https://www.mindstudio.ai/blog/claude-code-mcp-server-token-overhead/) · [DeployStack 2026-01-10](https://deploystack.io/blog/mcp-token-limits-the-hidden-cost-of-tool-overload) · [Code for Creatives 2026-03-14](https://codeforcreatives.com/blog/your-claude.md-file-is-too-long-and-its-making-claude-worse/) · [Thomas Wiegold 2026-03-09](https://thomas-wiegold.com/blog/claude-md-helpful-or-expensive-noise/)

### 3.2 信息重复 / 失效同步

- 你当前的 `notes/` 目录 + Obsidian vault `sessions/` + `.claude/projects/*/memory/` 三处可能记同一件事。
- Nick Lawson (Towards Data Science, 2026-04-17)："大多数实现做了 write + read，完全忽略 **manage**（pruning）"。
- 他的失败案例：OpenClaw 项目因 embedding 把正确数据误标 "faulty"，永久忽略 SmartThings 输入 → memory 不 prune 会反噬。

### 3.3 Memory Poisoning（新攻击面）

- Cisco Threat Intelligence（2026-04-01）记录：npm postinstall hook 可以写入 `~/.claude/projects/*/memory/MEMORY.md`，**永久性地把 Claude 的认知带偏**。
- **Anthropic v2.1.50 响应**：把 user memory 从 system prompt 移除 → 影响：任何依赖 system-prompt-injection 的工具（包括 claude-mem 的 SessionStart 注入模式）未来路径变不稳。

> Source: [blogs.cisco.com/ai/identifying-and-remediating-a-persistent-memory-compromise-in-claude-code](https://blogs.cisco.com/ai/identifying-and-remediating-a-persistent-memory-compromise-in-claude-code)

### 3.4 Context Rot（物理天花板）

- Chroma 2025 study：所有前沿模型在 ~100–120K tokens 后效果塌方，**无论窗口多大**（1M context 也救不了）。
- 同 session ~40 messages 后，instruction compliance 从 95% 降到 20–60%。
- 原话（Nick Lawson）："**you don't keep Claude Code open all day**"。

### 3.5 MEMORY.md 静默丢弃

- 前 200 行 / 25KB 启动加载，**超出部分不报警就丢了**。
- 你的 MEMORY.md Index 已 18 条，接近上限。

---

## 4. Obsidian 集成方案（三选一）

### 方案 A：Obsidian MCP 直连
- 代表：[iansinnott/obsidian-claude-code-mcp](https://github.com/iansinnott/obsidian-claude-code-mcp)（WebSocket）· [MarkusPfundstein/mcp-obsidian](https://github.com/MarkusPfundstein/mcp-obsidian)（REST wrapper）· [AlexW00/obsidian-rest-mcp](https://forum.obsidian.md/t/obsidian-rest-api-mcp-server/109211)（OpenAPI 自动生成）
- **代价**：+1500–3000 tokens/turn schema overhead
- **收益**：Claude 可以直接 activate note / 切 panel
- **适合**：每天高频让 Claude 读写 vault 的工作流

### 方案 B：文件系统桥接（无 MCP）【社区推荐】
- 做法：`cd ~/vault && claude` 或 `claude --add-dir ~/vault`
- 代表实践：Kenneth Reitz · James Donnelly · thirteen37（「**prefer skills over MCP servers**」）
- **代价**：无 Obsidian 原生 API
- **收益**：零 token overhead / git 版本控制 / 可审计
- **适合**：低频批量读写，有 `/checkpoint` `/learn` 脚本习惯

### 方案 C：只读 / 只写 / 双向
- 社区主流共识：**"Claude proposes, you dispose"**（Kenneth Reitz）
- CC **默认只读** vault；写限定在 `_claude/` 或 `projects/.claude-output/` 子目录
- **反模式**：让 Claude 污染 "authentic thinking" 笔记（dev.to/mibii 警告）

> Sources: [thirteen37 2026-04-09](https://thirteen37.github.io/engineering/2026/04/09/obsidian-claude.html) · [Kenneth Reitz 2026-03-06](https://kennethreitz.org/essays/2026-03-06-obsidian_vaults_and_claude_code) · [James Donnelly 2026-01-21](https://jamesdonnelly.dev/blog/obsidian-claude-code-workflow/) · [dev.to/mibii 2026-04-10](https://dev.to/mibii/claude-code-obsidian-build-a-second-brain-that-actually-thinks-d61)

---

## 5. 分层记忆模型（业界共识）

Hu et al. "Memory in the Age of AI Agents" (arXiv 2512.13564, 2025-12) + "Missing Knowledge Layer" (arXiv 2604.11364, 2026-04) 已把四层结构定为事实标准：

| 层 | 名称 | CC 对应 | 半衰期 |
|---|------|--------|-------|
| **L1** Working | in-context 200K 窗口 | 当前 session | 会话结束 |
| **L2** Episodic | 时序事件 | Obsidian `sessions/` | 7 天 |
| **L3** Semantic | 蒸馏知识 | CLAUDE.md + MEMORY.md + Obsidian knowledge/ | 69 天 |
| **L4** Procedural | 技能 / 行为 | `.claude/skills/` + `.claude/rules/` + hooks | 693 天 |

**Mem0 vs Zep 基准**（LongMemEval GPT-4o）：Zep 63.8% vs Mem0 49.0% —— 15 分差距来自 temporal knowledge graph（时间区间标记事实）。

**三条关键原则**：
1. **raw episodic 必须保留** — 总结会漂移，raw 是 ground truth
2. **semantic 必须人工筛** — 不是什么都进 MEMORY.md
3. **procedural 当代码对待** — git 版本控制、PR review

> Sources: [arXiv 2512.13564](https://www.aifasthub.com/papers/2512.13564) · [arXiv 2604.11364](https://arxiv.org/pdf/2604.11364) · [Atlan 2026-04-02](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)

---

## 6. Allen 的推荐栈（concrete action items）

### 6.1 删除

- **claude-mem 插件** — 原因：
  1. 功能与 auto-memory + Obsidian 三重重叠（它的 SessionStart 注入和 auto-memory 注入的是同一类信息）
  2. v2.1.50 后 system-prompt-injection 路径变不稳
  3. 4 个 MCP tool schema +1500–3000 tokens/turn overhead
  4. 近期 regression 多（v12.1.3/4/5/6 连续热修）
  5. 你有 Obsidian vault 作 L2/L3 记忆 → claude-mem 的跨 session 回看是 nice-to-have，不是必需

  **保留条件**：如果你确实每周 > 5 次需要"跨 session 检索 7 天前发生了什么"，才值得留。否则删。

- **Obsidian MCP**（如果装了） — 走文件系统桥接更经济

### 6.2 精简

- **`~/.claude/CLAUDE.md`** — 目标 < 80 行（你现在 ~50 行，已达标）
- **项目 `CLAUDE.md`** — 目标 < 80 行（Jarvis 当前 ~60 行，OK）
- **`MEMORY.md` (auto-memory)** — 目标 **< 150 行**（留 50 行 buffer 防 200 cap 截断）
- **MCP servers 审计** — 过去 2 周未调用的全关（用 `claude mcp list`）

### 6.3 保留 + 强化

- **auto-memory MEMORY.md** — 官方系统、手工可编辑、version-stable
- **Obsidian vault**（文件系统桥接模式） — L2 sessions + L3 knowledge
- **`/checkpoint` `/learn` workflow** — 已内化的习惯

### 6.4 新增（可选）

- **basic-memory MCP** — **仅当**你发现 auto-memory + Obsidian 都捕不到某类信息时，再评估加它
- **`.claude/rules/`** — 大 CLAUDE.md 拆分为路径匹配规则文件（例：`rules/voice.md` `rules/tests.md` `rules/git.md`）
- **定期 pruning（月度 checklist）**：
  - [ ] MEMORY.md ≤ 150 行
  - [ ] CLAUDE.md ≤ 80 行，删除已体现在代码的内容
  - [ ] auto-memory 中过去 60 天未触发的 feedback → Obsidian archive
  - [ ] MCP servers 审计（3–6 个为宜）
  - [ ] 让 Claude 自己审 MEMORY.md（meta-loop）

### 6.5 推荐最终栈（极简版）

```
┌─────────────────────────────────────────────────────┐
│ 规则层                                              │
│   ~/.claude/CLAUDE.md           <80 行，全局偏好    │
│   project/CLAUDE.md             <80 行，项目约定    │
│   project/.claude/rules/*.md    按路径匹配的细则    │
├─────────────────────────────────────────────────────┤
│ 状态层（Claude 自动维护）                           │
│   .claude/projects/*/memory/    <150 行 MEMORY.md   │
│     + typed files (user/feedback/project/reference) │
├─────────────────────────────────────────────────────┤
│ 知识层（手工精编）                                   │
│   ~/Obsidian Vault/<project>/   L2 sessions/        │
│                                 L3 knowledge/       │
│                                 _overview.md        │
│   通过 --add-dir 让 CC 读写，不装 MCP               │
├─────────────────────────────────────────────────────┤
│ 技能层                                              │
│   .claude/skills/               procedural 知识      │
│   .claude/hooks/                强制规则            │
└─────────────────────────────────────────────────────┘
```

**不装**：claude-mem · Obsidian MCP · memento-mcp · mem0 SaaS · supermemory

---

## 7. 不确定点 / 需进一步验证

1. **Anthropic v2.1.50 后 auto-memory 是否彻底离开 system prompt** — Cisco blog 说"removes"，但官方 docs 未更新，可能是部分移除
2. **MEMORY.md 硬上限 200 行** — Code for Creatives 实测，Anthropic docs 未明说，可能随版本变
3. **Claude 4.5+ 1M context 后 "lost in middle" 是否缓解** — 2026-02 后新数据未公开
4. **claude-mem FTS5+Chroma 检索命中率 vs Obsidian Smart Connections** — 无独立对比 benchmark
5. **"RAG is dead" 争议**（Karpathy vs thirteen37）— 个人 vault 规模下谁对尚无定论

---

## 核心引用（完整列表）

**官方**
- [How Claude remembers your project — code.claude.com](https://code.claude.com/docs/en/memory)
- [Memory tool — platform.claude.com](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- [Context editing — platform.claude.com](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Effective context engineering for AI agents — Anthropic, 2025-09-29](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Claude Cookbook: Context engineering (2026-03-20)](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools)

**膨胀 / 反模式**
- [MindStudio: MCP Token Overhead (2026-04-02)](https://www.mindstudio.ai/blog/claude-code-mcp-server-token-overhead/)
- [DeployStack: MCP Token Limits (2026-01-10)](https://deploystack.io/blog/mcp-token-limits-the-hidden-cost-of-tool-overload)
- [Code for Creatives: CLAUDE.md Too Long (2026-03-14)](https://codeforcreatives.com/blog/your-claude.md-file-is-too-long-and-its-making-claude-worse/)
- [Thomas Wiegold: Helpful or Expensive Noise (2026-03-09)](https://thomas-wiegold.com/blog/claude-md-helpful-or-expensive-noise/)
- [BSWEN: MCP Token Optimization (2026-03-23)](https://docs.bswen.com/blog/2026-03-23-mcp-token-optimization-claude-code)
- [vexp: Context Rot in Claude Code (2026-03-15)](https://vexp.dev/blog/context-rot-claude-code)
- [Cisco: Memory Compromise in Claude Code (2026-04-01)](https://blogs.cisco.com/ai/identifying-and-remediating-a-persistent-memory-compromise-in-claude-code)

**产品 / 生态**
- [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem)
- [mem0ai/mem0](https://github.com/mem0ai/mem0)
- [basicmachines-co/basic-memory](https://github.com/basicmachines-co/basic-memory)
- [modelcontextprotocol/servers — memory](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)
- [gannonh/memento-mcp (停滞)](https://github.com/gannonh/memento-mcp)
- [obra/superpowers](https://github.com/obra/superpowers)

**Obsidian 集成**
- [iansinnott/obsidian-claude-code-mcp](https://github.com/iansinnott/obsidian-claude-code-mcp)
- [thirteen37 Second Brain's Second Brain (2026-04-09)](https://thirteen37.github.io/engineering/2026/04/09/obsidian-claude.html)
- [Kenneth Reitz: Second Brain That Thinks Back (2026-03-06)](https://kennethreitz.org/essays/2026-03-06-obsidian_vaults_and_claude_code)
- [James Donnelly: Obsidian + Claude Code (2026-01-21)](https://jamesdonnelly.dev/blog/obsidian-claude-code-workflow/)
- [dev.to/mibii: Build a Second Brain (2026-04-10)](https://dev.to/mibii/claude-code-obsidian-build-a-second-brain-that-actually-thinks-d61)

**学术 / 分层记忆**
- [Hu et al.: Memory in the Age of AI Agents (arXiv 2512.13564)](https://www.aifasthub.com/papers/2512.13564)
- [Missing Knowledge Layer (arXiv 2604.11364)](https://arxiv.org/pdf/2604.11364)
- [Atlan: Memory Frameworks Comparison (2026-04-02)](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- [Nick Lawson: Memory for Autonomous LLM Agents (TDS, 2026-04-17)](https://towardsdatascience.com/a-practical-guide-to-memory-for-autonomous-llm-agents/)

---

**报告生成**：2026-04-17 15:10 PDT  
**对应 session**：`~/Documents/Obsidian Vault/jarvis/sessions/2026-04-17-s1.md`
